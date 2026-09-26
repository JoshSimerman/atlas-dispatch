"""Post-run verification evidence.

Three independent checks run against the diff the implementer actually
produced:

- **Surface prediction** (``check_allowlist``). The task's ``allowed_paths``
  and ``forbidden_paths`` describe the change surface the spec author expected.
  Violations are measured and reported, but they do not gate verification on
  their own. See docs/DESIGN_DECISIONS.md for why.
- **Protected paths** (``check_protected_paths``). A harness-level list of
  globs that no dispatched task may change. A task spec cannot lower it. Any
  match fails verification regardless of what the acceptance commands say.
- **Acceptance** (``run_acceptance_commands``). Shell commands from the spec,
  run inside the worktree. At least one must exist and every one must pass.

A run passes only when files changed, no protected path was touched, and at
least one acceptance command ran and every command passed.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from atlas_dispatch import import_provenance

LOGGER = logging.getLogger(__name__)
# Acceptance commands are usually a project's full test suite, which can take
# many minutes on a large repository. A default below the normal duration of
# the work it supervises does not detect hangs, it manufactures failures, so
# the default leaves generous headroom. Specs can lower it per task.
DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS = 3600
ACCEPTANCE_SLOW_WARNING_SECONDS = 300.0

# Harness-level protected paths. A change to any matching file fails
# verification no matter what the acceptance commands report, and a task spec
# cannot opt out. These defaults are illustrative; set
# ATLAS_DISPATCH_PROTECTED_PATTERNS in the environment that runs the harness to
# replace them for your repositories.
PROTECTED_PATTERNS_ENV = "ATLAS_DISPATCH_PROTECTED_PATTERNS"
PROTECTED_PATTERNS: tuple[str, ...] = (
    # Production configuration: a wrong value here reaches users directly.
    "configs/prod.*",
    # Schema migrations are hard to reverse once applied.
    "migrations/**",
    # Credentials and secrets must never be written by an agent.
    "secrets/**",
    # CI definitions decide what "green" means; an agent must not edit its own gate.
    ".github/workflows/**",
)


def protected_patterns(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the protected-path globs in force for this process.

    ``ATLAS_DISPATCH_PROTECTED_PATTERNS``, when set, REPLACES the defaults. It
    takes a newline- or comma-separated list of globs; set it to an empty
    string to disable the floor entirely. The value comes from the
    environment of the process running the harness, never from a task spec,
    so the author of a spec (possibly another agent) cannot lower it.
    """

    env = os.environ if environ is None else environ
    raw = env.get(PROTECTED_PATTERNS_ENV)
    if raw is None:
        return PROTECTED_PATTERNS
    return tuple(
        item.strip()
        for item in re.split(r"[\n,]", raw)
        if item.strip()
    )


AcceptanceClassification = Literal[
    "success",
    "acceptance_failed",
    "acceptance_collection_shrank",
    "acceptance_timeout",
    "stale_import_binding",
    "unknown",
]
AcceptanceOutcome = Literal["passed", "failed", "skipped", "not_attempted"]
PytestCounts = dict[str, int]
PytestCountsResult = PytestCounts | Literal["unavailable"]
PYTEST_COUNTS_UNAVAILABLE: Literal["unavailable"] = "unavailable"
_PYTEST_COUNT_KEYS = (
    "collected",
    "deselected",
    "errors",
    "passed",
    "failed",
    "xfailed",
    "skipped",
)
_PYTEST_EXPLICIT_COLLECTED_PATTERNS = (
    re.compile(r"\bcollected\s+(?P<count>\d+)\s+items?\b", re.IGNORECASE),
    re.compile(r"\b(?P<count>\d+)\s+(?:items?|tests?)\s+collected\b", re.IGNORECASE),
)
_PYTEST_OUTCOME_COUNT_RE = re.compile(
    r"\b(?P<count>\d+)\s+"
    r"(?P<outcome>passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b",
    re.IGNORECASE,
)
_PYTEST_TERMINAL_TIMING_RE = re.compile(
    r"\bin\s+\d+(?:\.\d+)?(?:ms|s| seconds?)\b", re.IGNORECASE
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class VerificationState(StrEnum):
    """Durable meaning of an acceptance-verification attempt.

    There is intentionally no ``UNKNOWN`` member. Missing, malformed, and
    future values fail validation instead of becoming a state a review gate
    could accidentally treat as truthy.
    """

    VERIFIED_PASS = "VERIFIED_PASS"
    VERIFIED_FAIL = "VERIFIED_FAIL"
    NOT_RUN_NO_CHANGE = "NOT_RUN_NO_CHANGE"


# The import-provenance guard's two refusal verdicts. No executed acceptance
# command can produce either value, so membership in this set means the guard
# refused before any acceptance command ran.
_PROVENANCE_REFUSAL_CLASSIFICATIONS = frozenset({"stale_import_binding", "unknown"})


@dataclass(frozen=True, kw_only=True)
class CheckResult:
    name: str
    passed: bool
    details: str = ""
    duration_seconds: float = 0.0
    classification: AcceptanceClassification = "success"
    pytest_counts: PytestCountsResult = PYTEST_COUNTS_UNAVAILABLE
    acceptance_min_collected: int | None = None


class _AcceptanceCommandResults(list[CheckResult]):
    def __init__(self, results: list[CheckResult], exit_codes: list[int | None]) -> None:
        super().__init__(results)
        self.exit_codes = exit_codes


@dataclass(kw_only=True)
class VerifyReport:
    changed_files_present: bool = False
    allowlist_passed: bool = False
    forbidden_violations: list[str] = field(default_factory=list)
    out_of_scope_paths: list[str] = field(default_factory=list)
    protected_path_violations: list[str] = field(default_factory=list)
    command_results: list[CheckResult] = field(default_factory=list)
    acceptance_skip_reason: list[str] | None = None

    @property
    def acceptance_outcome(self) -> AcceptanceOutcome:
        if not self.command_results:
            return "skipped"
        if all(
            check.classification in _PROVENANCE_REFUSAL_CLASSIFICATIONS
            for check in self.command_results
        ):
            return "not_attempted"
        if all(check.passed for check in self.command_results):
            return "passed"
        return "failed"

    @property
    def acceptance_not_attempted_reason(self) -> str:
        """The provenance guard's verdict and reason, when acceptance never ran.

        Empty when acceptance_outcome is not "not_attempted". The refusal must
        reach the durable record, not only stdout.
        """
        if self.acceptance_outcome != "not_attempted":
            return ""
        return "; ".join(check.details for check in self.command_results if check.details)

    @property
    def verification_state(self) -> VerificationState | None:
        """Return the discriminated state directly supported by this report.

        A skipped report is classified only for an empty diff; any other skip
        returns None, because the report cannot say more than that acceptance
        did not run.
        """

        if self.acceptance_outcome == "not_attempted":
            return None
        if self.acceptance_outcome == "passed" and self.passed:
            return VerificationState.VERIFIED_PASS
        if self.acceptance_outcome == "failed" or (
            self.acceptance_outcome == "passed" and self.protected_path_violations
        ):
            return VerificationState.VERIFIED_FAIL
        if self.acceptance_skip_reason and "no_files_changed" in (
            self.acceptance_skip_reason
        ):
            return VerificationState.NOT_RUN_NO_CHANGE
        return None

    @property
    def passed(self) -> bool:
        return (
            self.changed_files_present
            and not self.protected_path_violations
            and bool(self.command_results)
            and all(check.passed for check in self.command_results)
        )


def _path_matches(path: str, pattern: str) -> bool:
    """Match an allow/forbid glob without letting ``*`` or ``?`` cross ``/``.

    ``**`` remains the explicit cross-directory wildcard. All other pattern
    characters are literal, including square brackets used by paths such as
    Next.js dynamic-route directories.
    """
    translated: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            translated.append(".*")
            index += 2
            continue

        character = pattern[index]
        if character == "*":
            translated.append("[^/]*")
        elif character == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(character))
        index += 1

    return re.fullmatch("".join(translated), path) is not None


def check_allowlist(
    *,
    changed_files: list[str],
    allowed_paths: list[str],
    forbidden_paths: list[str],
) -> tuple[bool, list[str], list[str]]:
    """Return informational task-surface prediction measurements."""
    scoped_changed_files = [
        path for path in changed_files if not _is_tool_internal_path(path)
    ]
    forbidden_violations = [
        path
        for path in scoped_changed_files
        if any(_path_matches(path, pattern) for pattern in forbidden_paths)
    ]
    out_of_scope = [
        path
        for path in scoped_changed_files
        if not any(_path_matches(path, pattern) for pattern in allowed_paths)
    ]
    passed = not forbidden_violations and not out_of_scope
    return passed, forbidden_violations, out_of_scope


def check_protected_paths(
    *,
    changed_files: list[str],
    patterns: tuple[str, ...] | None = None,
) -> list[str]:
    """Return changed files that match a harness-level protected pattern."""

    active = protected_patterns() if patterns is None else patterns
    return [
        path
        for path in changed_files
        if not _is_tool_internal_path(path)
        and any(_path_matches(path, pattern) for pattern in active)
    ]


def _is_tool_internal_path(path: str) -> bool:
    """Recognize the exact worktree locations written by the harness."""
    normalized = path.replace("\\", "/").lstrip("/")
    return (
        normalized.startswith(".atlas-dispatch/codex-home/")
        or normalized == ".atlas-dispatch-mcp.json"
        or normalized == ".gemini/settings.json"
    )


def run_acceptance_commands(
    *,
    commands: list[str],
    cwd: Path,
    timeout_seconds: int = DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS,
    acceptance_min_collected: int | Mapping[str, int] | None = None,
    import_provenance_required: bool = False,
    run_dir: Path | None = None,
) -> list[CheckResult]:
    """Run acceptance commands and retain exact exit codes for cli.summary.json.

    Classification precedence is deliberately centralized here and in
    ``_run_acceptance_shell_command``: timeout, then stale import binding, then
    collection-floor failure, then the command's ordinary exit status. Thus a
    command has one verdict even when multiple controls fire. An ``unknown``
    provenance result also remains a pre-execution refusal rather than being
    relabelled as a collection result.
    """

    results: list[CheckResult] = []
    exit_codes: list[int | None] = []
    for command in commands:
        minimum_collected = _minimum_collected_for_command(
            acceptance_min_collected,
            command,
        )
        if (
            import_provenance_required
            and import_provenance.is_import_bearing_acceptance_command(command)
        ):
            provenance_result = import_provenance.run_import_provenance_check(
                cwd,
                acceptance_command=command,
                run_dir=run_dir,
            )
            if not provenance_result.passed:
                provenance_checks = _import_provenance_command_results(
                    provenance_result,
                    minimum_collected=minimum_collected,
                )
                results.extend(provenance_checks)
                provenance_exit_codes = getattr(provenance_checks, "exit_codes", [1])
                exit_codes.extend(provenance_exit_codes)
                print(
                    "[atlas-dispatch] import_provenance="
                    f"{provenance_result.verdict}; acceptance refused"
                )
                return _AcceptanceCommandResults(results, exit_codes)
        check, exit_code = _run_acceptance_shell_command(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            minimum_collected=minimum_collected,
        )
        results.append(check)
        exit_codes.append(exit_code)
    return _AcceptanceCommandResults(results, exit_codes)


def _run_acceptance_shell_command(
    *,
    command: str,
    cwd: Path,
    timeout_seconds: int,
    minimum_collected: int | None = None,
) -> tuple[CheckResult, int | None]:
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        duration_seconds = time.monotonic() - started
        _log_slow_acceptance_command(
            command=command,
            duration_seconds=duration_seconds,
        )
        exit_code = process.returncode
        pytest_counts = _parse_pytest_counts(stdout, stderr)
        classification, passed = _classify_completed_acceptance(
            exit_code=exit_code,
            pytest_counts=pytest_counts,
            minimum_collected=minimum_collected,
        )
        details = _format_command_details(stdout, stderr)
        if classification == "acceptance_collection_shrank":
            details = _format_collection_floor_failure(
                details=details,
                pytest_counts=pytest_counts,
                minimum_collected=minimum_collected,
            )
        return (
            CheckResult(
                name=command,
                passed=passed,
                details=details,
                duration_seconds=duration_seconds,
                classification=classification,
                pytest_counts=pytest_counts,
                acceptance_min_collected=minimum_collected,
            ),
            exit_code,
        )
    except subprocess.TimeoutExpired:
        elapsed_seconds = time.monotonic() - started
        if process is not None:
            _kill_process_tree(process)
            stdout, stderr = _communicate_after_kill(process)
        else:
            stdout, stderr = "", ""

        duration_seconds = time.monotonic() - started
        LOGGER.warning(
            "acceptance command timed out",
            extra={
                "acceptance_command": command,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "timeout_seconds": timeout_seconds,
            },
        )
        _log_slow_acceptance_command(
            command=command,
            duration_seconds=duration_seconds,
        )
        pytest_counts = _parse_pytest_counts(stdout, stderr)
        return (
            CheckResult(
                name=command,
                passed=False,
                details=_format_timeout_details(
                    timeout_seconds=timeout_seconds,
                    stdout=stdout,
                    stderr=stderr,
                ),
                duration_seconds=duration_seconds,
                classification="acceptance_timeout",
                pytest_counts=pytest_counts,
                acceptance_min_collected=minimum_collected,
            ),
            None,
        )


def _import_provenance_command_results(
    result: import_provenance.ImportProvenanceResult,
    *,
    minimum_collected: int | None = None,
) -> list[CheckResult]:
    classification = cast(AcceptanceClassification, result.verdict)
    check = CheckResult(
        name="import provenance guard",
        passed=False,
        details=import_provenance.format_import_provenance_failure(result),
        classification=classification,
        pytest_counts=PYTEST_COUNTS_UNAVAILABLE,
        acceptance_min_collected=minimum_collected,
    )
    return _AcceptanceCommandResults([check], [1])


def _minimum_collected_for_command(
    configured: int | Mapping[str, int] | None,
    command: str,
) -> int | None:
    if isinstance(configured, Mapping):
        return configured.get(command)
    return configured


def _parse_pytest_counts(
    stdout: str | None,
    stderr: str | None,
) -> PytestCountsResult:
    """Extract pytest's denominator and outcomes from captured summary text.

    A mapping is returned only when the output supplies (or lets us derive) a
    collected count. In particular, an empty pytest suite is represented by a
    mapping with ``collected=0``; output that cannot establish a denominator is
    the explicit string ``"unavailable"`` and can never masquerade as empty.
    """

    output = _ANSI_ESCAPE_RE.sub("", (stdout or "") + "\n" + (stderr or ""))
    counts = dict.fromkeys(_PYTEST_COUNT_KEYS, 0)
    explicit_collected: int | None = None
    terminal_outcomes: dict[str, int] = {}
    xpassed = 0
    recognized = False
    complete_terminal_summary = False

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        collection_line = False
        for pattern in _PYTEST_EXPLICIT_COLLECTED_PATTERNS:
            match = pattern.search(line)
            if match is not None:
                explicit_collected = int(match.group("count"))
                recognized = True
                collection_line = True
                break

        no_tests_ran = "no tests ran" in line.casefold()
        outcome_matches = list(_PYTEST_OUTCOME_COUNT_RE.finditer(line))
        terminal_line = bool(outcome_matches) and (
            _PYTEST_TERMINAL_TIMING_RE.search(line) is not None
            or (line.startswith("=") and line.endswith("="))
        )
        if no_tests_ran:
            recognized = True
            complete_terminal_summary = True
            explicit_collected = 0
        if terminal_line:
            recognized = True
            complete_terminal_summary = True

        if collection_line or terminal_line:
            target = terminal_outcomes if terminal_line else counts
            for match in outcome_matches:
                outcome = match.group("outcome").casefold()
                key = "errors" if outcome in {"error", "errors"} else outcome
                value = int(match.group("count"))
                if key == "xpassed":
                    xpassed = value
                else:
                    target[key] = value

    if not recognized:
        return PYTEST_COUNTS_UNAVAILABLE

    counts.update(terminal_outcomes)
    if explicit_collected is not None:
        counts["collected"] = explicit_collected
        return counts

    # Quiet pytest suppresses its collection header. A completed terminal
    # summary still determines the denominator from mutually exclusive outcomes
    # plus deselections. Do not derive from interrupted collection output: a
    # collection error is not itself a collected test.
    collection_interrupted = bool(
        re.search(r"errors? during collection|ERROR collecting", output, re.I)
    )
    if complete_terminal_summary and not collection_interrupted:
        counts["collected"] = (
            counts["passed"]
            + counts["failed"]
            + counts["errors"]
            + counts["skipped"]
            + counts["xfailed"]
            + counts["deselected"]
            + xpassed
        )
        return counts
    return PYTEST_COUNTS_UNAVAILABLE


def _classify_completed_acceptance(
    *,
    exit_code: int,
    pytest_counts: PytestCountsResult,
    minimum_collected: int | None,
) -> tuple[AcceptanceClassification, bool]:
    """Apply collection-floor precedence over failure and success exits."""

    if minimum_collected is not None and (
        pytest_counts == PYTEST_COUNTS_UNAVAILABLE
        or pytest_counts["collected"] < minimum_collected
    ):
        return "acceptance_collection_shrank", False
    if exit_code == 0:
        return "success", True
    return "acceptance_failed", False


def _format_collection_floor_failure(
    *,
    details: str,
    pytest_counts: PytestCountsResult,
    minimum_collected: int | None,
) -> str:
    assert minimum_collected is not None
    if pytest_counts == PYTEST_COUNTS_UNAVAILABLE:
        reason = (
            "acceptance collection floor not satisfied: pytest counts are "
            f"unavailable; required collected >= {minimum_collected}"
        )
    else:
        reason = (
            "acceptance collection shrank below its floor: "
            f"collected {pytest_counts['collected']} < {minimum_collected}"
        )
    return f"{reason}\n{details}" if details else reason


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill the shell subprocess and its process group when supported."""
    if process.poll() is not None:
        return

    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(process.pid), kill_signal)
        except ProcessLookupError:
            pass
        except OSError as exc:
            LOGGER.warning(
                "acceptance process group kill failed",
                extra={
                    "acceptance_pid": process.pid,
                    "error": str(exc),
                },
            )

    try:
        process.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        LOGGER.warning(
            "acceptance subprocess kill failed",
            extra={
                "acceptance_pid": process.pid,
                "error": str(exc),
            },
        )


def _communicate_after_kill(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            return "", ""
    return stdout or "", stderr or ""


def _format_command_details(stdout: str | None, stderr: str | None) -> str:
    return ((stdout or "") + (f"\n[stderr]\n{stderr}" if stderr else "")).strip()


def _format_timeout_details(
    *,
    timeout_seconds: int,
    stdout: str | None,
    stderr: str | None,
) -> str:
    details = [f"timed out after {timeout_seconds}s"]
    command_details = _format_command_details(stdout, stderr)
    if command_details:
        details.append(command_details)
    return "\n".join(details)


def _log_slow_acceptance_command(
    *,
    command: str,
    duration_seconds: float,
) -> None:
    if duration_seconds < ACCEPTANCE_SLOW_WARNING_SECONDS:
        return
    LOGGER.warning(
        "acceptance command exceeded slow threshold",
        extra={
            "acceptance_command": command,
            "elapsed_seconds": round(duration_seconds, 3),
            "threshold_seconds": ACCEPTANCE_SLOW_WARNING_SECONDS,
        },
    )
