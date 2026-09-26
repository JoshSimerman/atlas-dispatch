"""Coding-agent dispatcher; never auto-merges."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from atlas_dispatch import import_provenance
from atlas_dispatch.adapter import (
    CLIS,
    DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    AdapterResult,
    Classification,
    DispatchErrorKind,
    cli_doctor,
    inject_mcp_config,
    list_supported_clis,
    list_supported_models,
    resolve_invocation,
    resolve_model,
    run_cli,
)
from atlas_dispatch.git_exec import git_result_is_index_lock_contention, run_git
from atlas_dispatch.verify import (
    DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS,
    CheckResult,
    VerifyReport,
    check_allowlist,
    check_protected_paths,
    run_acceptance_commands,
)
from atlas_dispatch.worktree import (
    BranchPublishResult,
    Worktree,
    commit_all,
    create_worktree,
    is_git_repo,
    list_changed_files,
    publish_branch,
    worktree_path_for,
)

NO_CHANGE_SUGGESTED_ACTION = (
    "CLI returned 0 exit code but produced no commits. Likely cause: auth "
    "failure, model refusal, or context-window pre-emption. Verify CLI auth, "
    "read cli.stdout.txt for the model's own explanation, then redispatch."
)
ALLOW_DUPLICATE_RUN_ENV = "ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN"
ALLOW_CONSECUTIVE_DISPATCH_ENV = "ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH"
REUSE_POLICY_ALLOW = "allow"
REUSE_POLICY_NEVER = "never"
CONSECUTIVE_DISPATCH_CAP = 3
CONSECUTIVE_DISPATCH_CAP_REFUSAL_MARKER = "consecutive_dispatch_cap_refusal.json"
# A retry loop that redispatches the same task every few seconds is a runaway.
# Three attempts in five minutes is already that signal, while ordinary
# redispatches spread over hours or days never approach it.
CONSECUTIVE_DISPATCH_WINDOW = dt.timedelta(minutes=5)
# ---------------------------------------------------------------------------
# ABSOLUTE, CLOCK-INDEPENDENT RUNAWAY BACKSTOP
#
# The window cap above is time-based, and a time-based brake can be defeated by
# the very runaway it exists to stop. If run-directory names ever drift ahead of
# the wall clock (for example, a collision strategy that bumps the timestamp in
# the name), a loop faster than one dispatch per second pushes its own history
# "into the future", the window check stops seeing it, and the loop runs
# unbounded.
#
# This cap shares no mechanism with the window cap: it counts directories and
# never parses a timestamp, so no clock, drift or naming anomaly can disable it.
# A single dispatch id legitimately reaching this many runs is itself
# pathological.
ABSOLUTE_DISPATCH_RUN_CAP = int(
    os.environ.get("ATLAS_DISPATCH_ABSOLUTE_RUN_CAP", "100") or "100"
)
LOGGER = logging.getLogger(__name__)
PREDISPATCH_GIT_LOCK_CONTENTION_KIND = "git_lock_contention"
PREDISPATCH_GIT_LOCK_CONTENTION_SUGGESTED_ACTION = (
    "Transient pre-dispatch git index lock contention; retry or redispatch."
)
PREDISPATCH_GIT_INDEX_LOCK_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5)
PREDISPATCH_GIT_FETCH_CONNECTION_RETRY_DELAYS_SECONDS = (0.1, 0.2)
_PREDISPATCH_REPO_LOCKS_GUARD = threading.Lock()
_PREDISPATCH_REPO_LOCKS: dict[Path, threading.Lock] = {}
CONTEXT_FILE_READ_TIMEOUT_SECONDS = 5.0
CHANGED_FILES_UNKNOWN = "UNKNOWN"
CHANGED_FILES_UNKNOWN_PREFIX = f"{CHANGED_FILES_UNKNOWN}: "
POST_RUN_INCOMPLETE_MARKER = "post_run_incomplete"
POST_RUN_INCOMPLETE_CLASSIFICATION = "post_run_incomplete"
POST_RUN_INCOMPLETE_VERIFICATION_STATE = "POST_RUN_INCOMPLETE"
POST_RUN_HEARTBEAT_INTERVAL_SECONDS = 15.0
POST_RUN_HEARTBEAT_ARTIFACT = "post_run.json"


class PreDispatchGitSyncError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        classification: str | None = None,
        transient: bool = False,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.transient = transient
        self.suggested_action = suggested_action


@dataclass(frozen=True, kw_only=True)
class TaskSpec:
    id: str
    ticket_ref: str | None = None
    title: str
    target_repo: Path
    cli: str  # always populated; resolved from `model` if only that was given
    prompt_template: Path
    worktree_branch: str
    allowed_paths: list[str]
    forbidden_paths: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, object]] = field(default_factory=list)
    context_files: list[Path] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    acceptance_timeout_seconds: int = DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS
    acceptance_min_collected: int | dict[str, int] | None = None
    # Probe that pytest-bearing acceptance commands import this worktree's
    # packages (not a stale editable install). Default on; see
    # import_provenance.py for when to turn it off.
    check_import_provenance: bool = True
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    idle_timeout_seconds: int = DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS
    base_ref: str = "main"
    model: str | None = None
    reasoning_effort: str | None = None
    extra_prompt_vars: dict[str, str] = field(default_factory=dict)
    push_branch: bool = False
    detached_head: bool = False
    allow_destructive_branch_reset: bool = False
    protected_head_sha: str | None = None
    remote_name: str = "origin"
    runs_dir: Path | None = None
    spec_path: Path = Path()
    resolved_via: str = ""

    @property
    def reuse_policy(self) -> str:
        """One-attempt dispatch control, intentionally outside task identity."""

        return str(getattr(self, "_reuse_policy", REUSE_POLICY_ALLOW))


@dataclass(frozen=True, kw_only=True)
class McpRuntime:
    config_path: Path | None = None
    extra_env: dict[str, str] | None = None
    worktree_files: list[Path] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class ChangedFilesEvidence:
    """What this run actually learned about its changed-file set."""

    state: str
    files: tuple[str, ...] = ()
    reason: str = ""


@dataclass(kw_only=True)
class _DispatchProgress:
    cli_invoked: bool = False


def _write_text_atomic(path: Path, content: str) -> None:
    """Replace one critical artifact atomically and flush it before returning."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _write_text_atomic(path, json.dumps(dict(payload), indent=2))


class _PostRunHeartbeat:
    """Durable post-run liveness evidence with an intentionally stale failure mode.

    ``post_run.json`` is the authoritative liveness record.  The summary is the
    authoritative verdict record.  A hard-killed process leaves ``state=running``
    behind, but its heartbeat timestamp and worktree marker stop advancing; a
    supervisor can therefore leave live work alone without waiting on a hung
    post-run forever.
    """

    def __init__(
        self,
        *,
        run_dir: Path,
        worktree_path: Path,
        task: TaskSpec,
        heartbeat: Any | None,
    ) -> None:
        self.run_dir = run_dir
        self.task = task
        self.external_heartbeat = heartbeat
        self.started_at = dt.datetime.now(dt.UTC)
        self.started_monotonic = time.monotonic()
        self.last_heartbeat_at = self.started_at
        self.shutdown = threading.Event()
        self.thread_started = False
        self.marker_path = (
            worktree_path
            / ".atlas-dispatch"
            / f"post-run.{os.getpid()}.tmp"
        )
        self.thread = threading.Thread(
            target=self._run,
            name=f"atlas-dispatch-post-run-{task.id}",
            daemon=True,
        )

    def start(self) -> dict[str, object]:
        self._beat()
        self.thread.start()
        self.thread_started = True
        return self.snapshot(state="running")

    def finish(
        self,
        *,
        state: str,
        error: BaseException | None = None,
    ) -> dict[str, object]:
        self.shutdown.set()
        if self.thread_started:
            self.thread.join(timeout=2.0)
        ended_at = dt.datetime.now(dt.UTC)
        payload = self.snapshot(state=state, ended_at=ended_at, error=error)
        _write_json_atomic(self.run_dir / POST_RUN_HEARTBEAT_ARTIFACT, payload)
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass
        return payload

    def snapshot(
        self,
        *,
        state: str,
        ended_at: dt.datetime | None = None,
        error: BaseException | None = None,
    ) -> dict[str, object]:
        duration = time.monotonic() - self.started_monotonic
        payload: dict[str, object] = {
            "state": state,
            "classification": (
                "post_run_completed"
                if state == "completed"
                else POST_RUN_INCOMPLETE_CLASSIFICATION
            ),
            "started_at": self.started_at.isoformat(),
            "last_heartbeat_at": self.last_heartbeat_at.isoformat(),
            "ended_at": ended_at.isoformat() if ended_at is not None else "not_recorded",
            "duration_seconds": round(duration, 2),
            # This is the spec's outer CLI supervision budget. Acceptance has
            # its own independently recorded bound.
            "supervising_timeout_seconds": self.task.timeout_seconds,
            "acceptance_timeout_seconds": self.task.acceptance_timeout_seconds,
            "heartbeat_interval_seconds": POST_RUN_HEARTBEAT_INTERVAL_SECONDS,
            "heartbeat_artifact": str(
                (self.run_dir / POST_RUN_HEARTBEAT_ARTIFACT).resolve()
            ),
        }
        if error is not None:
            payload["error"] = f"{type(error).__name__}: {error}"
        return payload

    def _run(self) -> None:
        while not self.shutdown.wait(POST_RUN_HEARTBEAT_INTERVAL_SECONDS):
            self._beat()

    def _beat(self) -> None:
        self.last_heartbeat_at = dt.datetime.now(dt.UTC)
        beat = getattr(self.external_heartbeat, "beat", None)
        if callable(beat):
            beat()
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.touch()
        _write_json_atomic(
            self.run_dir / POST_RUN_HEARTBEAT_ARTIFACT,
            self.snapshot(state="running"),
        )


class _DispatchRunError(RuntimeError):
    def __init__(
        self,
        *,
        error: Exception,
        run_dir: Path,
        prompt: str,
        cli_invoked: bool,
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.run_dir = run_dir
        self.prompt = prompt
        self.cli_invoked = cli_invoked


def _acceptance_requires_import_provenance(
    commands: list[str], *, enabled: bool = True
) -> bool:
    if not enabled:
        return False
    for command in commands:
        if import_provenance.is_import_bearing_acceptance_command(command):
            return True
    return False


# A single-segment ``target_repo`` such as ``"my-service"`` is a repo NAME,
# resolved against code roots, so one spec works unchanged on any machine that
# follows the same layout. The roots default to ``~/code`` and can be set with
# ATLAS_DISPATCH_CODE_ROOTS (os.pathsep-separated, searched in order).
CODE_ROOTS_ENV = "ATLAS_DISPATCH_CODE_ROOTS"


def _is_single_segment_repo_name(value: str) -> bool:
    return (
        bool(value)
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
        and not value.startswith(".")
    )


def _code_roots() -> list[Path]:
    raw = os.environ.get(CODE_ROOTS_ENV, "").strip()
    if not raw:
        return [Path.home() / "code"]
    return [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]


def _conventional_repo_candidates(name: str) -> list[tuple[Path, str]]:
    return [(root / name, str(root / name)) for root in _code_roots()]


def _resolve_target_repo(value: str, resolve_relative: Callable[[str], Path]) -> Path:
    """Resolve ``target_repo``, honouring the single-segment repo-name form.

    Every other path-like field in a spec is relative to the spec file. A
    single-segment value (``"my-service"``) is instead treated as a repo NAME
    and resolved against the code roots (``~/code/<name>`` by default, or each
    root in ATLAS_DISPATCH_CODE_ROOTS in order), which is what makes a spec
    portable across machines.

    A relative path that actually exists still wins, so ``".."`` and
    ``"../my-service"`` keep their meaning.
    """

    relative = resolve_relative(value)
    if is_git_repo(relative):
        return relative
    if _is_single_segment_repo_name(value):
        for candidate, _ in _conventional_repo_candidates(value):
            if is_git_repo(candidate):
                return candidate.resolve()
    return relative


def load_task(spec_path: Path) -> TaskSpec:
    """Load a task spec from a JSON file.

    Path-like fields (`target_repo`, `prompt_template`, `context_files`,
    `runs_dir`) are resolved relative to the spec file's directory unless
    absolute. The (cli, model, reasoning_effort) triple may come from a
    registry entry under `model` (e.g. "codex/gpt-6-sol-high"), or be
    specified explicitly via `cli`, `model_id`, and `reasoning_effort`.
    """
    spec_path = spec_path.resolve()
    raw = json.loads(spec_path.read_text(encoding="utf-8"))

    base_dir = spec_path.parent

    def _resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (base_dir / path).resolve()

    target_repo = _resolve_target_repo(raw["target_repo"], _resolve)
    prompt_template = _resolve(raw["prompt_template"])
    context_files = [_resolve(p) for p in raw.get("context_files", [])]
    runs_dir_raw = raw.get("runs_dir")
    runs_dir = _resolve(runs_dir_raw) if runs_dir_raw else None
    forbidden_paths = list(raw.get("forbidden_paths", []))
    raw_reuse_policy = raw.get("reuse_policy")
    raw_no_reuse = raw.get("no_reuse")
    if raw_reuse_policy is not None and raw_reuse_policy not in {
        REUSE_POLICY_ALLOW,
        REUSE_POLICY_NEVER,
    }:
        raise ValueError(
            f"task spec {spec_path.name}: reuse_policy must be "
            f"{REUSE_POLICY_ALLOW!r} or {REUSE_POLICY_NEVER!r}"
        )
    if raw_no_reuse is not None and type(raw_no_reuse) is not bool:
        raise ValueError(
            f"task spec {spec_path.name}: no_reuse must be a JSON boolean"
        )
    requested_policies = {
        str(policy)
        for present, policy in (
            (raw_reuse_policy is not None, raw_reuse_policy),
            (
                raw_no_reuse is not None,
                REUSE_POLICY_NEVER if raw_no_reuse else REUSE_POLICY_ALLOW,
            ),
        )
        if present
    }
    if len(requested_policies) > 1:
        raise ValueError(
            f"task spec {spec_path.name}: conflicting reuse controls "
            f"{sorted(requested_policies)}"
        )
    reuse_policy = next(iter(requested_policies), REUSE_POLICY_ALLOW)

    cli = raw.get("cli")
    model_name = raw.get("model")
    model_id = raw.get("model_id")
    reasoning_effort = raw.get("reasoning_effort")
    resolved_via = ""

    if model_name and model_name in list_supported_models():
        # Registry-style: `model` field names a registry entry.
        definition = resolve_model(model_name)
        cli = definition.cli
        model_id = definition.model_id
        if reasoning_effort is None:
            reasoning_effort = definition.reasoning_effort
        resolved_via = f"model_registry:{model_name}"
    elif model_name and cli is None:
        # `model` was given but doesn't match the registry and no `cli` —
        # raise so the spec author gets a clear error.
        raise ValueError(
            f"task spec {spec_path.name}: `model={model_name!r}` is not a "
            f"registry entry and no `cli` was specified. "
            f"Supported registry models: {list_supported_models()}. "
            "Either pick a registry entry, or specify `cli` + `model_id` + "
            "`reasoning_effort` directly."
        )
    else:
        # Explicit shape: cli + model_id + reasoning_effort.
        cli = (cli or "codex").lower()
        resolved_via = "explicit"

    raw_ticket_ref = raw.get("ticket_ref")
    ticket_ref = (
        str(raw_ticket_ref).strip() if raw_ticket_ref is not None else None
    ) or None
    acceptance = list(raw.get("acceptance", []))
    raw_check_import_provenance = raw.get("check_import_provenance", True)
    if type(raw_check_import_provenance) is not bool:
        raise ValueError(
            f"task spec {spec_path.name}: check_import_provenance must be a "
            "JSON boolean"
        )

    task = TaskSpec(
        id=str(raw["id"]),
        ticket_ref=ticket_ref,
        title=str(raw["title"]),
        target_repo=target_repo,
        cli=str(cli).lower(),
        prompt_template=prompt_template,
        worktree_branch=str(raw["worktree_branch"]),
        allowed_paths=list(raw["allowed_paths"]),
        forbidden_paths=forbidden_paths,
        mcp_servers=list(raw.get("mcp_servers") or []),
        context_files=context_files,
        acceptance=acceptance,
        acceptance_timeout_seconds=int(
            raw.get("acceptance_timeout_seconds", DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS)
        ),
        acceptance_min_collected=_parse_acceptance_min_collected(
            raw,
            spec_path=spec_path,
            acceptance=acceptance,
        ),
        check_import_provenance=raw_check_import_provenance,
        timeout_seconds=int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        idle_timeout_seconds=int(
            raw.get("idle_timeout_seconds", DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS)
        ),
        base_ref=str(raw.get("base_ref", "main")),
        model=model_id,
        reasoning_effort=reasoning_effort,
        extra_prompt_vars=dict(raw.get("extra_prompt_vars") or {}),
        push_branch=bool(raw.get("push_branch") or False),
        detached_head=bool(raw.get("detached_head") or False),
        allow_destructive_branch_reset=(
            raw.get("allow_destructive_branch_reset") is True
        ),
        protected_head_sha=(
            str(raw.get("protected_head_sha") or "").strip() or None
        ),
        remote_name=str(raw.get("remote_name") or "origin"),
        runs_dir=runs_dir,
        spec_path=spec_path,
        resolved_via=resolved_via,
    )
    object.__setattr__(task, "_reuse_policy", reuse_policy)
    return task


def _parse_acceptance_min_collected(
    raw: Mapping[str, object],
    *,
    spec_path: Path,
    acceptance: list[str],
) -> int | dict[str, int] | None:
    """Validate the optional global or exact-command collection floor."""

    value = raw.get("acceptance_min_collected")
    if value is None:
        return None
    if type(value) is int:
        if value < 0:
            raise ValueError(
                f"task spec {spec_path.name}: acceptance_min_collected must be >= 0"
            )
        return value
    if not isinstance(value, Mapping):
        raise ValueError(
            f"task spec {spec_path.name}: acceptance_min_collected must be an "
            "integer or a mapping from acceptance command to integer"
        )

    parsed: dict[str, int] = {}
    for command, floor in value.items():
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"task spec {spec_path.name}: acceptance_min_collected mapping "
                "keys must be non-empty command strings"
            )
        if type(floor) is not int or floor < 0:
            raise ValueError(
                f"task spec {spec_path.name}: acceptance_min_collected for "
                f"{command!r} must be an integer >= 0"
            )
        parsed[command] = floor

    unknown_commands = sorted(set(parsed) - set(acceptance))
    if unknown_commands:
        raise ValueError(
            f"task spec {spec_path.name}: acceptance_min_collected names command(s) "
            f"not present in acceptance: {unknown_commands}"
        )
    return parsed


def render_prompt(task: TaskSpec) -> str:
    """Render the prompt template with task variables and inlined context.

    Substitutes `{{name}}` placeholders. Refuses to dispatch a prompt
    with any unfilled placeholder.
    """
    if not task.prompt_template.is_file():
        raise FileNotFoundError(f"prompt template not found: {task.prompt_template}")
    template_text = task.prompt_template.read_text(encoding="utf-8")
    context_block = _build_context_block(task.context_files)

    _worktree_path = worktree_path_for(
        task.target_repo, task.worktree_branch
    ).resolve()

    variables: dict[str, str] = {
        "task_id": task.id,
        "task_title": task.title,
        "worktree_branch": task.worktree_branch,
        "worktree_path": str(_worktree_path),
        "target_repo": str(task.target_repo),
        "cli": task.cli,
        "model": task.model or "",
        "reasoning_effort": task.reasoning_effort or "",
        "base_ref": task.base_ref,
        "allowed_paths_block": "\n".join(f"- {p}" for p in task.allowed_paths)
        or "- (none — task should not modify any file)",
        "forbidden_paths_block": "\n".join(f"- {p}" for p in task.forbidden_paths)
        or "- (no per-task forbidden paths beyond your project rules)",
        "acceptance_block": "\n".join(f"- `{c}`" for c in task.acceptance)
        or "- (no acceptance commands defined; verify will refuse to pass)",
        "context_block": context_block,
    }
    variables.update(task.extra_prompt_vars or {})

    rendered = template_text
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", _prompt_variable_text(value))

    if "read timed out after" in context_block and "{{context_block}}" not in template_text:
        rendered += ("\n\n" if rendered else "") + context_block

    # Scan the TEMPLATE, not the rendered prompt.
    #
    # This check exists to catch a half-rendered TEMPLATE: a slot the spec
    # never filled. Scanning `rendered` would also see every substituted VALUE,
    # and a value is data. `context_files` inline whole source files, and a
    # source file (a test fixture, a template engine) may legitimately contain
    # a literal `{{...}}`. Failing on that blames a template that is clean.
    #
    # Exempting fenced code is not enough on its own: an inlined file with an
    # odd number of fences desynchronises fence tracking for everything after
    # it. Not scanning values at all removes that whole class of false alarm.
    declared = set(variables)
    unfilled = [
        placeholder
        for placeholder in _find_unfilled_prompt_placeholders(template_text)
        if placeholder[2:-2] not in declared
    ]
    if unfilled:
        raise ValueError(
            f"prompt template {task.prompt_template} has unfilled placeholders: "
            f"{sorted(set(unfilled))}. Provide them in the task spec's "
            "extra_prompt_vars or remove them from the template."
        )

    return rendered


def _prompt_variable_text(value: object) -> str:
    """Render one prompt variable as substitution text.

    `extra_prompt_vars` comes from task-spec JSON, so a value can be any JSON type
    even though the built-in variables are all strings. Passing a non-str straight
    to `str.replace()` raises `TypeError`, which would abort the dispatch before
    the CLI is ever invoked.

    Numbers are legitimate prompt variables (a retry round, a line budget), so
    coerce rather than reject; containers are JSON-encoded because a Python repr
    is not what a prompt should carry.
    """

    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    return str(value)


def _find_unfilled_prompt_placeholders(rendered: str) -> list[str]:
    unfilled: list[str] = []
    opening_fence_char: str | None = None
    opening_fence_length = 0

    for line in rendered.splitlines():
        stripped_line = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped_line)
        if opening_fence_char is None and fence_match:
            opening_fence_char = fence_match.group(0)[0]
            opening_fence_length = len(fence_match.group(0))
            continue
        if opening_fence_char is not None:
            if (
                fence_match
                and fence_match.group(0)[0] == opening_fence_char
                and len(fence_match.group(0)) >= opening_fence_length
                and not stripped_line[fence_match.end() :].strip()
            ):
                opening_fence_char = None
                opening_fence_length = 0
            continue

        line_without_inline_code = re.sub(r"`.*?`", "", line)
        unfilled.extend(
            re.findall(
                r"\{\{[a-zA-Z_][a-zA-Z0-9_]*\}\}",
                line_without_inline_code,
            )
        )

    return unfilled


def _build_context_block(context_files: list[Path]) -> str:
    chunks: list[str] = []
    for path in context_files:
        label = str(path)
        try:
            body = _read_context_file_bounded(path)
        except FileNotFoundError:
            chunks.append(f"## {label}\n\n(file not found at {path})")
            continue
        if body is None:
            chunks.append(
                f"## {label}\n\n(context file skipped: read timed out after "
                f"{CONTEXT_FILE_READ_TIMEOUT_SECONDS:g} seconds at {path})"
            )
            continue
        longest_backtick_run = max(
            (len(match.group(0)) for match in re.finditer(r"`+", body)),
            default=0,
        )
        fence = "`" * max(3, longest_backtick_run + 1)
        chunks.append(f"## {label}\n\n{fence}\n{body}\n{fence}")
    return "\n\n".join(chunks)


def _read_context_file_bounded(path: Path) -> str | None:
    outcome: list[str | Exception] = []

    def read() -> None:
        try:
            if not path.is_file() and not path.is_fifo():
                raise FileNotFoundError(path)
            outcome.append(path.read_text(encoding="utf-8"))
        except Exception as exc:
            outcome.append(exc)

    reader = threading.Thread(
        target=read,
        name=f"atlas-context-read:{path.name}",
        daemon=True,
    )
    reader.start()
    reader.join(CONTEXT_FILE_READ_TIMEOUT_SECONDS)
    if reader.is_alive():
        # A read blocked in the kernel (for example on a wedged network
        # filesystem) cannot be cancelled, so this daemon thread may leak.
        # Proceeding is intentional: one bad context file must not hang dispatch.
        return None
    result = outcome[0]
    if isinstance(result, Exception):
        raise result
    return result


def _default_runs_dir(task: TaskSpec) -> Path:
    return task.target_repo / ".atlas-dispatch" / "runs" / task.id


def _prepare_mcp_runtime(
    cli: str,
    worktree_path: Path,
    mcp_servers: list[dict[str, object]],
) -> McpRuntime | None:
    if not mcp_servers:
        return None

    cli_lower = cli.lower()
    if cli_lower == "codex":
        codex_home = (worktree_path / ".atlas-dispatch" / "codex-home").resolve()
        config_path = _write_codex_config(codex_home, mcp_servers)
        _extend_git_info_exclude(worktree_path, [".atlas-dispatch/"])
        return McpRuntime(
            extra_env={"CODEX_HOME": str(codex_home)},
            worktree_files=[config_path],
        )

    if cli_lower == "gemini":
        settings_path = _write_gemini_settings(worktree_path, mcp_servers)
        _extend_git_info_exclude(worktree_path, [".gemini/"])
        return McpRuntime(worktree_files=[settings_path])

    config_path = _write_legacy_mcp_config(worktree_path, mcp_servers)
    _extend_git_info_exclude(worktree_path, [".atlas-dispatch-mcp.json"])
    return McpRuntime(config_path=config_path, worktree_files=[config_path])


def _render_mcp_servers(
    mcp_servers: list[dict[str, object]]
) -> dict[str, dict[str, object]]:
    rendered_servers: dict[str, dict[str, object]] = {}
    for raw_server in mcp_servers:
        server = dict(raw_server)
        name = str(server.pop("name"))
        server.setdefault("transport", "stdio")
        rendered_servers[name] = server
    return rendered_servers


def _write_legacy_mcp_config(
    worktree_path: Path, mcp_servers: list[dict[str, object]]
) -> Path:
    rendered_servers = _render_mcp_servers(mcp_servers)
    config_path = (worktree_path / ".atlas-dispatch-mcp.json").resolve()
    config_path.write_text(
        json.dumps({"mcpServers": rendered_servers}, indent=2), encoding="utf-8"
    )
    return config_path


def _write_mcp_config(
    worktree_path: Path, mcp_servers: list[dict[str, object]]
) -> Path | None:
    if not mcp_servers:
        return None
    return _write_legacy_mcp_config(worktree_path, mcp_servers)


def _write_gemini_settings(
    workspace: Path, mcp_servers: list[dict[str, object]]
) -> Path:
    rendered_servers = _render_mcp_servers(mcp_servers)
    settings_path = (workspace / ".gemini" / "settings.json").resolve()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"mcpServers": rendered_servers}, indent=2), encoding="utf-8"
    )
    return settings_path


def _write_codex_config(home_dir: Path, mcp_servers: list[dict[str, object]]) -> Path:
    home_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, rendered_server in _render_mcp_servers(mcp_servers).items():
        server = dict(rendered_server)
        command = str(server.get("command", ""))
        raw_args = server.get("args") or []
        args = raw_args if isinstance(raw_args, (list, tuple)) else [raw_args]

        lines.append(f"[mcp_servers.{_toml_key(name)}]")
        lines.append(f"command = {_toml_dump_str(command)}")
        lines.append(
            "args = ["
            + ", ".join(_toml_dump_str(str(arg)) for arg in args)
            + "]"
        )

        raw_env = server.get("env")
        if raw_env is not None:
            env = dict(cast(Mapping[object, object], raw_env))
            env_items = ", ".join(
                f"{_toml_key(str(key))} = {_toml_dump_str(str(value))}"
                for key, value in env.items()
            )
            lines.append(f"env = {{ {env_items} }}" if env_items else "env = {}")
        lines.append("")

    config_path = home_dir / "config.toml"
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path.resolve()


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return _toml_dump_str(value)


def _toml_dump_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _extend_git_info_exclude(worktree_path: Path, patterns: list[str]) -> None:
    if not patterns:
        return

    exclude_path = _git_info_exclude_path(worktree_path)
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = (
        exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    )
    existing_lines = existing_text.splitlines()
    missing = [pattern for pattern in patterns if pattern not in existing_lines]
    if not missing:
        return

    prefix = "" if not existing_text or existing_text.endswith(("\n", "\r")) else "\n"
    with exclude_path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(missing) + "\n")


def _git_info_exclude_path(worktree_path: Path) -> Path:
    dot_git = worktree_path / ".git"
    if dot_git.is_dir():
        return dot_git / "info" / "exclude"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        prefix = "gitdir:"
        if text.lower().startswith(prefix):
            git_dir = Path(text[len(prefix) :].strip())
            if not git_dir.is_absolute():
                git_dir = (worktree_path / git_dir).resolve()
            common_dir_file = git_dir / "commondir"
            if common_dir_file.is_file():
                common_dir = Path(common_dir_file.read_text(encoding="utf-8").strip())
                if not common_dir.is_absolute():
                    common_dir = (git_dir / common_dir).resolve()
                return common_dir / "info" / "exclude"
            return git_dir / "info" / "exclude"
    return dot_git / "info" / "exclude"


def _expected_review_deliverable_path(
    task: TaskSpec,
    *,
    workspace: Path,
) -> Path | None:
    """Resolve the Gemini reviewer's expected review file from agreeing spec declarations."""
    if task.cli != "gemini":
        return None

    raw_output_path = task.extra_prompt_vars.get("review_output_path")
    if not raw_output_path:
        return None

    declared_output_path = Path(raw_output_path).expanduser()
    if declared_output_path.is_absolute():
        return None

    resolved_workspace = workspace.resolve()
    expected_path = (resolved_workspace / declared_output_path).resolve()
    if not expected_path.is_relative_to(resolved_workspace):
        return None

    acceptance_paths: list[Path] = []
    for acceptance_command in task.acceptance:
        try:
            command = shlex.split(acceptance_command)
        except ValueError:
            continue
        if len(command) != 3 or command[:2] != ["test", "-f"]:
            continue
        accepted_path = Path(command[2]).expanduser()
        if accepted_path.is_absolute():
            return None
        resolved_accepted_path = (resolved_workspace / accepted_path).resolve()
        if not resolved_accepted_path.is_relative_to(resolved_workspace):
            return None
        acceptance_paths.append(resolved_accepted_path)

    if not acceptance_paths or any(
        accepted_path != expected_path for accepted_path in acceptance_paths
    ):
        return None

    return expected_path


def dispatch(
    spec_path: Path,
    *,
    heartbeat: Any | None = None,
    no_reuse: bool = False,
) -> int:
    task = load_task(spec_path)
    if no_reuse:
        object.__setattr__(task, "_reuse_policy", REUSE_POLICY_NEVER)
    runs_root = task.runs_dir or _default_runs_dir(task)

    try:
        return _dispatch_loaded_task(
            task=task,
            runs_root=runs_root,
            heartbeat=heartbeat,
        )
    except Exception as exc:
        # A loaded task gives us enough identity to persist an honest failed-run
        # record even when validation or prompt rendering fails before a worktree
        # or CLI exists. Invalid JSON cannot be attributed to a TaskSpec and still
        # raises normally.
        failure = exc if isinstance(exc, _DispatchRunError) else None
        error = failure.error if failure is not None else exc
        run_dir = _write_dispatch_exception_run(
            task=task,
            runs_root=runs_root,
            error=error,
            cli_invoked=failure.cli_invoked if failure is not None else False,
            run_dir=failure.run_dir if failure is not None else None,
            prompt=failure.prompt if failure is not None else "",
        )
        _raise_dispatch_error_with_artifact(error, run_dir)


def _dispatch_loaded_task(
    *,
    task: TaskSpec,
    runs_root: Path,
    heartbeat: Any | None = None,
) -> int:

    if not is_git_repo(task.target_repo):
        raise ValueError(
            f"target_repo is not a git repository root: {task.target_repo}"
        )
    if task.cli not in list_supported_clis():
        print(
            f"[atlas-dispatch] warning: cli={task.cli!r} is not in the registry; "
            f"will attempt to invoke it directly. Supported: {list_supported_clis()}"
        )

    # Render (and read context files) before taking the lock: a slow
    # filesystem read must not hold the per-task lock.
    prompt = render_prompt(task)
    prepared_base_ref: str | None = None
    with _dispatch_runs_lock(runs_root):
        consecutive_dispatch_override_applied = False
        # BACKSTOP FIRST, and deliberately before anything that reads a clock:
        # if every time-based control is defeated, this one still counts.
        absolute_runs = _total_run_dir_count(runs_root)
        if absolute_runs >= ABSOLUTE_DISPATCH_RUN_CAP:
            if os.environ.get(ALLOW_CONSECUTIVE_DISPATCH_ENV, "").strip() == "1":
                consecutive_dispatch_override_applied = True
                message = (
                    "[atlas-dispatch] absolute run cap override honoured: "
                    f"{ALLOW_CONSECUTIVE_DISPATCH_ENV}=1; task={task.id} "
                    f"run_dirs={absolute_runs} cap={ABSOLUTE_DISPATCH_RUN_CAP}"
                )
                print(message)
                LOGGER.warning(message)
            else:
                message = (
                    "[atlas-dispatch] ABSOLUTE RUN CAP REACHED: "
                    f"task={task.id} run_dirs={absolute_runs} "
                    f"cap={ABSOLUTE_DISPATCH_RUN_CAP} runs_root={runs_root}; "
                    "CLI invocation refused. This id has dispatched more times "
                    "than any healthy task ever should — treat it as a runaway "
                    "and find why the caller keeps redispatching it. Set "
                    f"{ALLOW_CONSECUTIVE_DISPATCH_ENV}=1 to override."
                )
                print(message)
                LOGGER.error(message)
                return _escalate_consecutive_dispatch_cap(
                    task=task,
                    runs_root=runs_root,
                    prior_runs=[],
                    cap=ABSOLUTE_DISPATCH_RUN_CAP,
                    window=CONSECUTIVE_DISPATCH_WINDOW,
                    prompt=prompt,
                )
        cap_evaluation = _recent_consecutive_dispatch_runs(
            runs_root=runs_root,
            task=task,
            window=CONSECUTIVE_DISPATCH_WINDOW,
        )
        if cap_evaluation is None and not consecutive_dispatch_override_applied:
            # FAIL CLOSED. If this branch dispatched anyway, the cheapest way
            # past a runaway guard would be to break its inputs. An unreadable
            # runs_root is rare and visible; an unbounded loop is neither.
            message = (
                "[atlas-dispatch] consecutive-dispatch cap could not be "
                f"evaluated for task={task.id} base_ref={task.base_ref}; "
                "CLI invocation refused (fail-closed). Repair or clear "
                f"{runs_root}, or set {ALLOW_CONSECUTIVE_DISPATCH_ENV}=1."
            )
            print(message)
            LOGGER.error(message)
            return _escalate_consecutive_dispatch_cap(
                task=task,
                runs_root=runs_root,
                prior_runs=[],
                cap=CONSECUTIVE_DISPATCH_CAP,
                window=CONSECUTIVE_DISPATCH_WINDOW,
                prompt=prompt,
            )
        elif cap_evaluation is not None and len(cap_evaluation) >= CONSECUTIVE_DISPATCH_CAP:
            if os.environ.get(ALLOW_CONSECUTIVE_DISPATCH_ENV, "").strip() == "1":
                consecutive_dispatch_override_applied = True
                prior_paths = ", ".join(str(path.resolve()) for path in cap_evaluation)
                message = (
                    "[atlas-dispatch] consecutive-dispatch override honoured: "
                    f"{ALLOW_CONSECUTIVE_DISPATCH_ENV}=1; CLI invocation allowed "
                    f"for task={task.id} base_ref={task.base_ref} "
                    f"consecutive_recent_runs={len(cap_evaluation)} "
                    f"cap={CONSECUTIVE_DISPATCH_CAP} "
                    f"window_seconds={int(CONSECUTIVE_DISPATCH_WINDOW.total_seconds())} "
                    f"prior_run_paths=[{prior_paths}]"
                )
                print(message)
                LOGGER.warning(message)
            else:
                return _escalate_consecutive_dispatch_cap(
                    task=task,
                    runs_root=runs_root,
                    prior_runs=cap_evaluation,
                    cap=CONSECUTIVE_DISPATCH_CAP,
                    window=CONSECUTIVE_DISPATCH_WINDOW,
                    prompt=prompt,
                )
        duplicate_run_override_applied = (
            os.environ.get(ALLOW_DUPLICATE_RUN_ENV, "").strip() == "1"
        )
        if duplicate_run_override_applied and not consecutive_dispatch_override_applied:
            print(
                "[atlas-dispatch] duplicate-run override honored: "
                f"{ALLOW_DUPLICATE_RUN_ENV}=1; CLI invocation allowed "
                f"for task={task.id} base_ref={task.base_ref}"
            )
        if task.reuse_policy == REUSE_POLICY_NEVER:
            print(
                "[atlas-dispatch] run reuse disabled explicitly: "
                f"task={task.id} base_ref={task.base_ref}; a fresh CLI run is required"
            )
        if (
            task.reuse_policy != REUSE_POLICY_NEVER
            and not duplicate_run_override_applied
            and not consecutive_dispatch_override_applied
        ):
            try:
                prepared_base_ref = _pre_dispatch_sync_base_ref(
                    target_repo=task.target_repo,
                    base_ref=task.base_ref,
                    remote_name=task.remote_name,
                )
            except PreDispatchGitSyncError:
                # This preflight exists only to establish duplicate evidence.
                # Normal dispatch below remains authoritative and preserves its
                # existing failure/artifact behavior when synchronization fails.
                prepared_base_ref = None
            source_base_sha = (
                _resolve_source_base_sha(task.target_repo, prepared_base_ref)
                if prepared_base_ref is not None
                else None
            )
            prior_success = (
                _prior_successful_run(
                    runs_root=runs_root,
                    task=task,
                    source_base_sha=source_base_sha,
                    rendered_prompt=prompt,
                )
                if source_base_sha is not None
                else None
            )
            if prior_success is not None:
                prior_run, prior_summary = prior_success
                confirmed_source_base_sha = _resolve_source_base_sha(
                    task.target_repo,
                    cast(str, prepared_base_ref),
                )
                if confirmed_source_base_sha == source_base_sha:
                    print(
                        "[atlas-dispatch] DUPLICATE RUN SUPPRESSED: "
                        f"task={task.id} prior_run_dir={prior_run.resolve()} "
                        f"base_ref={task.base_ref} "
                        f"source_base_sha={source_base_sha}; no CLI was invoked"
                    )
                    return _write_duplicate_satisfied_run(
                        task=task,
                        runs_root=runs_root,
                        prior_run=prior_run,
                        prior_summary=prior_summary,
                        source_base_sha=source_base_sha,
                    )
        return _dispatch_new_run(
            task=task,
            runs_root=runs_root,
            prepared_base_ref=prepared_base_ref,
            prompt=prompt,
            heartbeat=heartbeat,
        )


@contextmanager
def _dispatch_runs_lock(runs_root: Path) -> Iterator[None]:
    """Serialize one dispatch id so concurrent attempts observe completed runs."""

    runs_root.mkdir(parents=True, exist_ok=True)
    lock_path = runs_root.parent / f".{runs_root.name}.dispatch.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _total_run_dir_count(runs_root: Path) -> int:
    """Count every run dir under one dispatch id. No clock, no name parsing.

    Deliberately independent of `_recent_consecutive_dispatch_runs`: a runaway
    can defeat that control through its timestamp inputs, so the backstop must
    not share them. Counts, unlike clocks, cannot drift.
    An unreadable runs_root returns the cap itself, so blindness refuses.
    """

    if not runs_root.exists():
        return 0
    try:
        return sum(1 for path in runs_root.iterdir() if path.is_dir())
    except OSError as exc:
        LOGGER.warning(
            "absolute run cap could not read runs_root %s: %s; treating as "
            "at-cap (fail-closed)",
            runs_root,
            exc,
        )
        return ABSOLUTE_DISPATCH_RUN_CAP


def _recent_consecutive_dispatch_runs(
    *,
    runs_root: Path,
    task: TaskSpec,
    window: dt.timedelta,
    now: dt.datetime | None = None,
) -> list[Path] | None:
    """Return recent consecutive run dirs for this task/base_ref, or None on doubt."""

    if not runs_root.exists():
        # A first dispatch has no history. This is the ONE safe kind of "nothing
        # to see": absence of the directory, not inability to read it.
        return []
    try:
        candidates = sorted(
            (path for path in runs_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError as exc:
        # Genuinely blind: the directory is there and cannot be read, so no
        # statement about runaway history can be made. The caller REFUSES.
        LOGGER.warning(
            "consecutive-dispatch cap could not read runs_root %s: %s",
            runs_root,
            exc,
        )
        return None

    reference_time = now or dt.datetime.now(dt.UTC)
    matches: list[Path] = []
    for candidate in candidates:
        timestamp = _parse_run_dir_timestamp(candidate.name)
        if timestamp is None:
            # SKIP the unreadable directory; do NOT abandon the whole
            # evaluation. Returning None here would let one junk directory
            # switch the cap off entirely.
            LOGGER.warning(
                "consecutive-dispatch cap skipping malformed run directory %s",
                candidate,
            )
            continue
        age = reference_time - timestamp
        if age < -window:
            # A future-dated directory is EVIDENCE OF A RUNAWAY (or a clock
            # fault), never a reason to stop counting. It counts toward the cap.
            LOGGER.warning(
                "consecutive-dispatch cap counting implausibly future run "
                "directory %s as runaway evidence "
                "(reference_time=%s window_seconds=%s)",
                candidate,
                reference_time.isoformat(),
                int(window.total_seconds()),
            )
            matches.append(candidate)
            continue
        if age > window:
            break

        try:
            prior_task = json.loads(
                (candidate / "task.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            LOGGER.warning(
                "consecutive-dispatch cap skipping unreadable task evidence "
                "in %s: %s",
                candidate,
                exc,
            )
            continue
        if not isinstance(prior_task, dict):
            LOGGER.warning(
                "consecutive-dispatch cap skipping non-object task evidence in %s",
                candidate,
            )
            continue

        prior_id = prior_task.get("id")
        prior_base_ref = prior_task.get("base_ref", "main")
        if str(prior_id) == task.id and str(prior_base_ref) == task.base_ref:
            matches.append(candidate)
            continue
        break
    return matches


def _parse_run_dir_timestamp(name: str) -> dt.datetime | None:
    # `-NNN` is the same-second collision suffix written by _new_run_dir; the
    # instant is carried by the part before it.
    base = name.split("-", 1)[0]
    try:
        return dt.datetime.strptime(base, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def _escalate_consecutive_dispatch_cap(
    *,
    task: TaskSpec,
    runs_root: Path,
    prior_runs: list[Path],
    cap: int,
    window: dt.timedelta,
    prompt: str,
) -> int:
    prior_paths = ", ".join(str(path.resolve()) for path in prior_runs)
    message = (
        "[atlas-dispatch] CONSECUTIVE DISPATCH CAP REACHED: "
        f"task={task.id} base_ref={task.base_ref} "
        f"consecutive_recent_runs={len(prior_runs)} cap={cap} "
        f"window_seconds={int(window.total_seconds())} "
        f"prior_run_paths=[{prior_paths}]; "
        "CLI invocation refused. "
        f"Set {ALLOW_CONSECUTIVE_DISPATCH_ENV}=1 to override intentionally."
    )
    print(message)
    LOGGER.error(message)
    return _write_consecutive_cap_refusal_run(
        task=task,
        runs_root=runs_root,
        prior_runs=prior_runs,
        cap=cap,
        window=window,
        prompt=prompt,
    )


def _prior_successful_run(
    *,
    runs_root: Path,
    task: TaskSpec,
    source_base_sha: str,
    rendered_prompt: str,
) -> tuple[Path, dict[str, Any]] | None:
    """Return the newest verified run with identical immutable work evidence."""

    try:
        candidates = sorted(
            (path for path in runs_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return None

    for candidate in candidates:
        try:
            summary = json.loads(
                (candidate / "cli.summary.json").read_text(encoding="utf-8")
            )
            prior_task = json.loads(
                (candidate / "task.json").read_text(encoding="utf-8")
            )
            prior_prompt = (candidate / "prompt.md").read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(summary, dict) or not isinstance(prior_task, dict):
            continue
        classification = summary.get("classification")
        if not isinstance(classification, dict):
            continue
        if classification.get("kind") != DispatchErrorKind.SUCCESS.value:
            continue
        if summary.get("verify_passed") is not True:
            continue
        if summary.get("source_base_sha") != source_base_sha:
            continue
        if prior_task != _serialized_task(task):
            continue
        if prior_prompt != rendered_prompt:
            continue
        return candidate, summary
    return None


def _serialized_task(task: TaskSpec) -> dict[str, Any]:
    """Return the exact JSON-compatible task identity persisted in task.json."""

    serialized = json.loads(json.dumps(asdict(task), default=str))
    if not isinstance(serialized, dict):  # pragma: no cover - dataclass invariant
        raise TypeError("serialized TaskSpec was not an object")
    return cast(dict[str, Any], serialized)


def _resolve_source_base_sha(target_repo: Path, base_ref: str) -> str | None:
    """Resolve a source ref to a full commit SHA, or fail open for dispatch."""

    result = _run_git(
        target_repo,
        ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
    )
    source_base_sha = result.stdout.strip()
    if (
        result.returncode != 0
        or re.fullmatch(r"[0-9a-fA-F]{40}", source_base_sha) is None
    ):
        return None
    return source_base_sha.lower()


def _new_run_dir(runs_root: Path) -> Path:
    """Create a timestamped run dir without overwriting a same-second attempt.

    A same-second collision adds a `-NNN` SUFFIX; it must never advance the
    timestamp in the NAME. Advancing it would make the directory name a lie
    about when the run happened, and a loop faster than 1/s would drift those
    names arbitrarily far into the future, blinding the time-window cap. The
    name must stay bound to real time.
    """

    timestamp = dt.datetime.now(dt.UTC).replace(microsecond=0)
    base = timestamp.strftime("%Y%m%dT%H%M%SZ")
    attempt = 0
    while True:
        name = base if attempt == 0 else f"{base}-{attempt:03d}"
        run_dir = runs_root / name
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            attempt += 1
            continue
        return run_dir


def _normalized_changed_files_unknown_reason(reason: str) -> str:
    normalized = " ".join(str(reason).split())
    return normalized[:1000] or "changed-file discovery did not run"


def _unknown_changed_files_evidence(reason: str) -> ChangedFilesEvidence:
    return ChangedFilesEvidence(
        state="unknown",
        reason=_normalized_changed_files_unknown_reason(reason),
    )


def _write_changed_files_evidence(
    run_dir: Path,
    evidence: ChangedFilesEvidence,
) -> None:
    if evidence.state == "unknown":
        content = CHANGED_FILES_UNKNOWN_PREFIX + evidence.reason
    elif evidence.state in {"empty", "observed"}:
        content = "\n".join(evidence.files)
    else:  # pragma: no cover - internal invariant
        raise ValueError(f"unsupported changed-files state: {evidence.state}")
    (run_dir / "changed_files.txt").write_text(content, encoding="utf-8")


def _known_changed_files_evidence(changed: list[str]) -> ChangedFilesEvidence:
    return ChangedFilesEvidence(
        state="observed" if changed else "empty",
        files=tuple(changed),
    )


def _read_changed_files_evidence(path: Path) -> ChangedFilesEvidence:
    if not path.is_file():
        return _unknown_changed_files_evidence(
            f"changed_files_artifact_missing: {path}"
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _unknown_changed_files_evidence(
            "changed_files_artifact_unreadable: "
            f"{type(exc).__name__}: {exc}"
        )
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return ChangedFilesEvidence(state="empty")
    if len(lines) == 1 and lines[0] == CHANGED_FILES_UNKNOWN:
        return _unknown_changed_files_evidence(
            "legacy unknown marker did not include a reason"
        )
    if len(lines) == 1 and lines[0].startswith(CHANGED_FILES_UNKNOWN_PREFIX):
        return _unknown_changed_files_evidence(
            lines[0].removeprefix(CHANGED_FILES_UNKNOWN_PREFIX)
        )
    if any(
        line == CHANGED_FILES_UNKNOWN
        or line.startswith(CHANGED_FILES_UNKNOWN_PREFIX)
        for line in lines
    ):
        return _unknown_changed_files_evidence(
            "changed_files_artifact_mixed_unknown_marker_with_paths"
        )
    return ChangedFilesEvidence(state="observed", files=tuple(lines))


def _set_changed_files_summary(
    summary: dict[str, Any],
    evidence: ChangedFilesEvidence,
) -> None:
    summary["changed_files_state"] = evidence.state
    if evidence.state == "unknown":
        summary["changed_files"] = CHANGED_FILES_UNKNOWN
        summary["changed_files_unknown_reason"] = evidence.reason
    else:
        summary["changed_files"] = list(evidence.files)
        summary.pop("changed_files_unknown_reason", None)


def _changed_files_report_lines(evidence: ChangedFilesEvidence) -> list[str]:
    if evidence.state == "unknown":
        return [f"**UNKNOWN** — {evidence.reason}"]
    return [f"- `{path}`" for path in evidence.files] or ["(none)"]


def _create_no_cli_run_artifact_shell(
    *,
    task: TaskSpec,
    runs_root: Path,
    prompt: str,
    changed_files_unknown_reason: str,
) -> Path:
    """Create the common complete artifact shell for a no-CLI attempt."""

    run_dir = _new_run_dir(runs_root)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (run_dir / "task.json").write_text(
        json.dumps(asdict(task), indent=2, default=str), encoding="utf-8"
    )
    (run_dir / "cli.stdout.txt").write_text("", encoding="utf-8")
    (run_dir / "cli.stderr.txt").write_text("", encoding="utf-8")
    _write_changed_files_evidence(
        run_dir,
        _unknown_changed_files_evidence(changed_files_unknown_reason),
    )
    return run_dir


def _write_consecutive_cap_refusal_run(
    *,
    task: TaskSpec,
    runs_root: Path,
    prior_runs: list[Path],
    cap: int,
    window: dt.timedelta,
    prompt: str,
) -> int:
    """Persist a complete attempt record refused by the consecutive-run cap."""

    run_dir = _create_no_cli_run_artifact_shell(
        task=task,
        runs_root=runs_root,
        prompt=prompt,
        changed_files_unknown_reason=(
            "cli_not_invoked: consecutive_dispatch_cap_reached"
        ),
    )
    prior_run_paths = [str(path.resolve()) for path in prior_runs]
    count = len(prior_runs)
    window_seconds = int(window.total_seconds())
    suggested_action = (
        "Dispatch was refused before CLI invocation after the consecutive-run "
        f"cap was reached. Inspect the repeated-dispatch cause, or set "
        f"{ALLOW_CONSECUTIVE_DISPATCH_ENV}=1 for an intentional Nth CLI run."
    )
    retry_policy = {
        "retry": False,
        "status": "stop_and_investigate",
        "reason": "consecutive_dispatch_cap_reached",
    }
    cap_evidence = {
        "refused": True,
        "task_id": task.id,
        "base_ref": task.base_ref,
        "consecutive_recent_runs": count,
        "cap": cap,
        "window_seconds": window_seconds,
        "prior_run_paths": prior_run_paths,
        "cli_invoked": False,
    }
    # Write the narrow refusal marker first. If the richer summary is truncated
    # or unreadable after a crash, consumers can still read the failure reason
    # and fail closed on retry without inferring anything from directory count.
    (run_dir / CONSECUTIVE_DISPATCH_CAP_REFUSAL_MARKER).write_text(
        json.dumps(
            {
                "classification": "consecutive_dispatch_cap_reached",
                "retry_policy": retry_policy,
                "consecutive_dispatch_cap": cap_evidence,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    summary: dict[str, Any] = {
        "cli": task.cli,
        "exit_code": 1,
        "duration_seconds": 0.0,
        "timed_out": False,
        "command": [],
        "classification": {
            "kind": "consecutive_dispatch_cap_reached",
            "suggested_action": suggested_action,
            "matched_pattern": None,
        },
        "cli_invoked": False,
        # This is downstream policy, not another attempt counter. The dispatcher
        # has already proved the 3-in-300s condition; a caller should honour
        # that verdict and stop retrying instead of independently recounting.
        "retry_policy": retry_policy,
        "consecutive_dispatch_cap": cap_evidence,
        "verify_passed": False,
        "acceptance_failed": True,
        "acceptance_outcome": "skipped",
        "verified_head_sha": "",
        "acceptance_commands": [],
        "acceptance_skip_reason": ["consecutive_dispatch_cap_reached"],
    }
    _set_changed_files_summary(
        summary,
        _read_changed_files_evidence(run_dir / "changed_files.txt"),
    )
    (run_dir / "cli.summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    prior_lines = [f"- `{path}`" for path in prior_run_paths]
    report_lines = [
        f"# Run report: {task.id} — {task.title}",
        "",
        f"- CLI: `{task.cli}`",
        f"- Resolved via: `{task.resolved_via or '(unspecified)'}`",
        f"- Branch: `{task.worktree_branch}`",
        f"- Target repo: `{task.target_repo}`",
        (
            f"- Model: `{task.model or '(default)'}` "
            f"reasoning=`{task.reasoning_effort or '(default)'}`"
        ),
        "- CLI invocation: **REFUSED**",
        "- CLI exit: `1` (timed_out=False)",
        "- Duration: 0.0s",
        "",
        "## Consecutive-dispatch cap",
        "",
        f"- Task: `{task.id}`",
        f"- Base ref: `{task.base_ref}`",
        f"- Consecutive recent runs: **{count}**",
        f"- Cap: **{cap}**",
        f"- Window: **{window_seconds} seconds**",
        "- No CLI was invoked for this attempt.",
        "- Retry policy: **stop retrying and investigate**.",
        f"- Suggested action: {suggested_action}",
        "",
        "### Prior run paths",
        "",
        *prior_lines,
        "",
        "## Classification",
        "",
        "- Kind: **consecutive_dispatch_cap_reached**",
        *_context_timeout_report_lines(run_dir),
        "",
        "## Changed files",
        "",
        "**UNKNOWN** — CLI was not invoked: consecutive_dispatch_cap_reached.",
        "",
        "## Acceptance commands",
        "",
        "- Acceptance outcome: **SKIPPED**",
        "- Dispatch was refused before CLI invocation.",
        "",
        "## Overall",
        "",
        "- Verify passed: **False**",
    ]
    (run_dir / "report.md").write_text(
        "\n".join(report_lines), encoding="utf-8"
    )
    print(f"[atlas-dispatch] run_dir={run_dir}")
    return 1


def _write_duplicate_satisfied_run(
    *,
    task: TaskSpec,
    runs_root: Path,
    prior_run: Path,
    prior_summary: dict[str, Any],
    source_base_sha: str,
) -> int:
    """Persist a complete attempt record satisfied by a prior successful run."""

    prompt = _read_text_artifact(prior_run / "prompt.md")
    if not prompt:
        prompt = (
            f"# Duplicate dispatch attempt: {task.id}\n\n"
            f"Satisfied by prior run {prior_run.resolve()} at base_ref "
            f"{task.base_ref}. No CLI was invoked.\n"
        )
    run_dir = _create_no_cli_run_artifact_shell(
        task=task,
        runs_root=runs_root,
        prompt=prompt,
        changed_files_unknown_reason=(
            "cli_not_invoked: awaiting prior-run changed-files evidence"
        ),
    )

    prior_run_path = str(prior_run.resolve())
    summary: dict[str, Any] = {
        "cli": task.cli,
        "exit_code": 0,
        "duration_seconds": 0.0,
        "timed_out": False,
        "command": [],
        "classification": {
            "kind": DispatchErrorKind.SUCCESS.value,
            "suggested_action": (
                "Satisfied by a prior verified run with identical task, prompt, "
                "and source commit evidence; no CLI was invoked."
            ),
            "matched_pattern": None,
        },
        "cli_invoked": False,
        "reuse_policy": task.reuse_policy,
        "run_reuse": {
            "reused": True,
            "kind": "prior_verified_run",
            "cli_invoked": False,
            "prior_run_path": prior_run_path,
        },
        "satisfied_by_prior_run": prior_run_path,
        "duplicate_run": {
            "prior_run_directory": prior_run.name,
            "prior_run_path": prior_run_path,
            "base_ref": task.base_ref,
            "source_base_sha": source_base_sha,
            "worktree_branch": task.worktree_branch,
            "allowed_paths": task.allowed_paths,
            "cli_invoked": False,
        },
        "source_base_sha": source_base_sha,
    }
    for key in (
        "verify_passed",
        "acceptance_failed",
        "acceptance_outcome",
        "verified_head_sha",
        "acceptance_commands",
        "acceptance_skip_reason",
        "protected_path_violations",
        "review_model_evidence",
    ):
        if key in prior_summary:
            summary[key] = prior_summary[key]
    summary.setdefault("verify_passed", False)
    summary.setdefault("acceptance_failed", True)
    summary.setdefault("acceptance_outcome", "skipped")
    summary.setdefault("verified_head_sha", "")
    summary.setdefault("acceptance_commands", [])
    summary.setdefault("acceptance_skip_reason", ["satisfied_by_prior_run"])
    summary.setdefault("protected_path_violations", [])
    changed_evidence = _read_changed_files_evidence(prior_run / "changed_files.txt")
    _write_changed_files_evidence(run_dir, changed_evidence)
    _set_changed_files_summary(summary, changed_evidence)
    (run_dir / "cli.summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    _copy_prior_run_artifact(prior_run / "branch_publish.json", run_dir)
    for diff_path in prior_run.glob("combined-*.diff"):
        _copy_prior_run_artifact(diff_path, run_dir)

    verify_passed = summary["verify_passed"] is True
    _write_duplicate_satisfied_report(
        run_dir=run_dir,
        task=task,
        prior_run=prior_run,
        changed_evidence=changed_evidence,
        source_base_sha=source_base_sha,
        verify_passed=verify_passed,
        acceptance_outcome=str(summary["acceptance_outcome"]),
    )
    print(f"[atlas-dispatch] run_dir={run_dir}")
    return 0 if verify_passed else 1


def _read_text_artifact(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _copy_prior_run_artifact(source: Path, run_dir: Path) -> None:
    try:
        content = source.read_bytes()
    except OSError:
        return
    try:
        (run_dir / source.name).write_bytes(content)
    except OSError as exc:
        LOGGER.warning("could not copy prior run artifact %s: %s", source, exc)


def _write_duplicate_satisfied_report(
    *,
    run_dir: Path,
    task: TaskSpec,
    prior_run: Path,
    changed_evidence: ChangedFilesEvidence,
    source_base_sha: str,
    verify_passed: bool,
    acceptance_outcome: str,
) -> None:
    file_lines = _changed_files_report_lines(changed_evidence)
    lines = [
        f"# Run report: {task.id} — {task.title}",
        "",
        f"- CLI: `{task.cli}`",
        f"- Resolved via: `{task.resolved_via or '(unspecified)'}`",
        f"- Branch: `{task.worktree_branch}`",
        f"- Target repo: `{task.target_repo}`",
        f"- Worktree: `{worktree_path_for(task.target_repo, task.worktree_branch)}`",
        (
            f"- Model: `{task.model or '(default)'}` "
            f"reasoning=`{task.reasoning_effort or '(default)'}`"
        ),
        "- CLI invocation: **SKIPPED**",
        "- CLI exit: `0` (timed_out=False)",
        "- Duration: 0.0s",
        "",
        "## Duplicate-run idempotence",
        "",
        f"- SATISFIED BY prior run: `{prior_run.resolve()}`",
        f"- Matching base_ref: `{task.base_ref}`",
        f"- Matching source commit: `{source_base_sha}`",
        f"- Matching worktree branch: `{task.worktree_branch}`",
        "- Matching task record and rendered prompt.",
        "- No CLI was invoked for this attempt.",
        "",
        "## Classification",
        "",
        "- Kind: **success**",
        *_context_timeout_report_lines(run_dir),
        "",
        "## Changed files",
        "",
        *file_lines,
        "",
        "## Allowlist check",
        "",
        "- Reused from the prior successful run named above.",
        "",
        "## Acceptance commands",
        "",
        f"- Acceptance outcome: **{acceptance_outcome.upper()}**",
        "- Reused from the prior successful run; acceptance was not re-run.",
        "",
        "## Branch publication",
        "",
        "- Reused from the prior successful run when present.",
        "",
        "## Overall",
        "",
        f"- Verify passed: **{verify_passed}**",
    ]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _write_dispatch_exception_run(
    *,
    task: TaskSpec,
    runs_root: Path,
    error: Exception,
    cli_invoked: bool,
    run_dir: Path | None = None,
    prompt: str = "",
) -> Path:
    """Persist an honest run record when dispatch aborts before diff discovery."""

    run_dir = run_dir or _new_run_dir(runs_root)
    prompt_path = run_dir / "prompt.md"
    if not prompt_path.is_file():
        prompt_path.write_text(prompt, encoding="utf-8")
    task_path = run_dir / "task.json"
    if not task_path.is_file():
        task_path.write_text(
            json.dumps(asdict(task), indent=2, default=str),
            encoding="utf-8",
        )
    for artifact_name in ("cli.stdout.txt", "cli.stderr.txt"):
        artifact_path = run_dir / artifact_name
        if not artifact_path.is_file():
            artifact_path.write_text("", encoding="utf-8")
    # Callers locate an exception run through this durable artifact path. No
    # rescue ref was created, so the honest payload is an empty array.
    rescue_path = run_dir / "rescue_refs.json"
    if not rescue_path.is_file():
        rescue_path.write_text("[]", encoding="utf-8")

    changed_path = run_dir / "changed_files.txt"
    changed_evidence = _read_changed_files_evidence(changed_path)
    if changed_evidence.state == "unknown":
        stage = "diff_not_observed_after_cli_invocation" if cli_invoked else "cli_not_invoked"
        changed_evidence = _unknown_changed_files_evidence(
            f"{stage}: dispatch_exception: {type(error).__name__}: {error}"
        )
        _write_changed_files_evidence(run_dir, changed_evidence)

    summary_path = run_dir / "cli.summary.json"
    try:
        raw_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw_summary = {}
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    post_run = summary.get("post_run")
    if (
        summary.get(POST_RUN_INCOMPLETE_MARKER) is True
        and isinstance(post_run, dict)
        and post_run.get("state") in {"running", "incomplete"}
    ):
        # The post-run handler already persisted the more precise failure. Do
        # not flatten it into dispatch_exception or acceptance_failed: an
        # absent acceptance verdict is not a failing acceptance verdict.
        summary["dispatch_error"] = f"{type(error).__name__}: {error}"
        _set_changed_files_summary(summary, changed_evidence)
        branch_reconciliation = _reconcile_published_branch(task)
        summary["branch_reconciliation"] = branch_reconciliation
        _write_json_atomic(summary_path, summary)
        _write_post_run_report(run_dir=run_dir, task=task, summary=summary)
        print(
            "[atlas-dispatch] post_run_incomplete "
            f"task={task.id} cli_invoked={cli_invoked} run_dir={run_dir}: "
            f"{type(error).__name__}: {error}"
        )
        return run_dir
    classification_kind = "dispatch_exception"
    suggested_action = "Fix the dispatch error and redispatch."
    if isinstance(error, PreDispatchGitSyncError) and error.classification:
        classification_kind = error.classification
        suggested_action = error.suggested_action or suggested_action
        summary["transient"] = error.transient
    summary.update(
        {
            "cli": task.cli,
            "exit_code": 1,
            "timed_out": False,
            "command": summary.get("command") or [],
            "classification": {
                "kind": classification_kind,
                "suggested_action": suggested_action,
                "matched_pattern": None,
            },
            "cli_invoked": cli_invoked,
            "dispatch_error": f"{type(error).__name__}: {error}",
            "verify_passed": False,
            "acceptance_failed": True,
            "acceptance_outcome": "skipped",
            # Explicitly "not a verdict". It prevents a consumer from reading
            # the verify_passed=False boolean as VERIFIED_FAIL for a dispatch
            # that never ran acceptance.
            "verification_state": "UNKNOWN",
            "acceptance_commands": [],
            "acceptance_skip_reason": ["dispatch_exception"],
        }
    )
    summary.pop(POST_RUN_INCOMPLETE_MARKER, None)
    _set_changed_files_summary(summary, changed_evidence)

    branch_reconciliation = _reconcile_published_branch(task)
    summary["branch_reconciliation"] = branch_reconciliation
    if branch_reconciliation.get("published") is True:
        branch_publish = {
            "remote_name": branch_reconciliation["remote_name"],
            "requested_remote_ref": task.worktree_branch,
            "remote_ref": branch_reconciliation["remote_ref"],
            "pushed": True,
            "state": "published_before_dispatch_exception",
            "reason": (
                "Remote branch was observed; publication is not attributable "
                "to this failed attempt."
            ),
            "return_code": 0,
            "stdout": "",
            "stderr": "",
            "error": "",
            "publication_attribution": "preexisting_or_unknown",
            "remote_head_sha": branch_reconciliation.get("remote_head_sha", ""),
            "nonzero_diff_vs_base": branch_reconciliation.get(
                "nonzero_diff_vs_base"
            ),
        }
        (run_dir / "branch_publish.json").write_text(
            json.dumps(branch_publish, indent=2),
            encoding="utf-8",
        )
    _write_json_atomic(summary_path, summary)
    _write_dispatch_exception_report(
        run_dir=run_dir,
        task=task,
        summary=summary,
        changed_evidence=changed_evidence,
        branch_reconciliation=branch_reconciliation,
    )
    print(
        "[atlas-dispatch] dispatch_exception "
        f"task={task.id} cli_invoked={cli_invoked} run_dir={run_dir}: "
        f"{type(error).__name__}: {error}"
    )
    return run_dir


def _raise_dispatch_error_with_artifact(error: Exception, run_dir: Path) -> None:
    message = f"{error}\nRescue artifact: {run_dir / 'rescue_refs.json'}"
    if isinstance(error, PreDispatchGitSyncError):
        raise PreDispatchGitSyncError(
            message,
            classification=error.classification,
            transient=error.transient,
            suggested_action=error.suggested_action,
        ) from error
    raise RuntimeError(message) from error


def _reconcile_published_branch(task: TaskSpec) -> dict[str, Any]:
    remote_ref = f"refs/heads/{task.worktree_branch}"
    try:
        remote_result = _run_git(
            task.target_repo,
            ["ls-remote", "--heads", task.remote_name, remote_ref],
        )
    except Exception as exc:
        return {
            "state": "unknown",
            "published": None,
            "remote_name": task.remote_name,
            "remote_ref": task.worktree_branch,
            "reason": f"remote branch lookup raised {type(exc).__name__}: {exc}",
        }
    if remote_result.returncode != 0:
        return {
            "state": "unknown",
            "published": None,
            "remote_name": task.remote_name,
            "remote_ref": task.worktree_branch,
            "reason": f"remote branch lookup failed: {_git_result_error(remote_result)}",
        }
    remote_line = remote_result.stdout.strip().splitlines()
    if not remote_line:
        return {
            "state": "not_published",
            "published": False,
            "remote_name": task.remote_name,
            "remote_ref": task.worktree_branch,
            "reason": "requested branch was not present on the remote",
        }
    remote_head_sha = remote_line[0].split(maxsplit=1)[0]
    if re.fullmatch(r"[0-9a-fA-F]{40}", remote_head_sha) is None:
        return {
            "state": "unknown",
            "published": None,
            "remote_name": task.remote_name,
            "remote_ref": task.worktree_branch,
            "reason": "remote branch lookup returned an invalid commit id",
        }
    diff_result = _run_git(
        task.target_repo,
        ["diff", "--quiet", f"{task.base_ref}...{remote_head_sha}"],
    )
    nonzero_diff: bool | None
    if diff_result.returncode == 0:
        nonzero_diff = False
    elif diff_result.returncode == 1:
        nonzero_diff = True
    else:
        nonzero_diff = None
    return {
        "state": (
            "published_work_exists_contents_unread"
            if nonzero_diff is True
            else "published_diff_empty"
            if nonzero_diff is False
            else "published_diff_state_unknown"
        ),
        "published": True,
        "remote_name": task.remote_name,
        "remote_ref": task.worktree_branch,
        "remote_head_sha": remote_head_sha.lower(),
        "base_ref": task.base_ref,
        "nonzero_diff_vs_base": nonzero_diff,
        "reason": (
            "remote branch has a non-zero diff; work exists but changed-file "
            "contents were not observed by this run"
            if nonzero_diff is True
            else "remote branch exists; changed-file contents were not observed by this run"
        ),
    }


def _write_dispatch_exception_report(
    *,
    run_dir: Path,
    task: TaskSpec,
    summary: dict[str, Any],
    changed_evidence: ChangedFilesEvidence,
    branch_reconciliation: dict[str, Any],
) -> None:
    classification = summary.get("classification")
    kind = (
        str(classification.get("kind") or "dispatch_exception")
        if isinstance(classification, dict)
        else "dispatch_exception"
    )
    branch_state = str(branch_reconciliation.get("state") or "unknown")
    lines = [
        f"# Run report: {task.id} — {task.title}",
        "",
        f"- CLI: `{task.cli}`",
        f"- Branch: `{task.worktree_branch}`",
        f"- Target repo: `{task.target_repo}`",
        f"- CLI invoked: **{summary.get('cli_invoked') is True}**",
        "- CLI exit: `1` (timed_out=False)",
        "",
        "## Classification",
        "",
        f"- Kind: **{kind}**",
        f"- Error: {summary.get('dispatch_error')}",
        "",
        "## Changed files",
        "",
        *_changed_files_report_lines(changed_evidence),
        "",
        "## Branch reconciliation",
        "",
        f"- State: **{branch_state}**",
        f"- Remote ref: `{task.remote_name}/{task.worktree_branch}`",
        f"- Non-zero diff vs `{task.base_ref}`: "
        f"**{branch_reconciliation.get('nonzero_diff_vs_base')}**",
        f"- Evidence: {branch_reconciliation.get('reason')}",
        "",
        "## Acceptance commands",
        "",
        "- Acceptance outcome: **SKIPPED**",
        "- Dispatch did not reach a verifiable build.",
        "",
        "## Overall",
        "",
        "- Verify passed: **False**",
    ]
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _dispatch_new_run(
    *,
    task: TaskSpec,
    runs_root: Path,
    prompt: str,
    prepared_base_ref: str | None = None,
    heartbeat: Any | None = None,
) -> int:
    run_dir = _new_run_dir(runs_root)
    progress = _DispatchProgress()
    try:
        return _dispatch_new_run_in_dir(
            task=task,
            run_dir=run_dir,
            prompt=prompt,
            progress=progress,
            prepared_base_ref=prepared_base_ref,
            heartbeat=heartbeat,
        )
    except Exception as exc:
        raise _DispatchRunError(
            error=exc,
            run_dir=run_dir,
            prompt=prompt,
            cli_invoked=progress.cli_invoked,
        ) from exc


def _dispatch_new_run_in_dir(
    *,
    task: TaskSpec,
    run_dir: Path,
    prompt: str,
    progress: _DispatchProgress,
    prepared_base_ref: str | None = None,
    heartbeat: Any | None = None,
) -> int:

    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (run_dir / "task.json").write_text(
        json.dumps(asdict(task), indent=2, default=str), encoding="utf-8"
    )

    print(f"[atlas-dispatch] task={task.id} cli={task.cli} resolved_via={task.resolved_via}")
    print(f"[atlas-dispatch] model={task.model or '(default)'} "
          f"reasoning={task.reasoning_effort or '(default)'}")
    print(f"[atlas-dispatch] target_repo={task.target_repo}")
    print(f"[atlas-dispatch] branch={task.worktree_branch}")
    print(f"[atlas-dispatch] run_dir={run_dir}")

    worktree_base_ref = (
        prepared_base_ref
        if prepared_base_ref is not None
        else _pre_dispatch_sync_base_ref(
            target_repo=task.target_repo,
            base_ref=task.base_ref,
            remote_name=task.remote_name,
        )
    )
    source_base_sha = _resolve_source_base_sha(
        task.target_repo,
        worktree_base_ref,
    )

    worktree = create_worktree(
        repo_root=task.target_repo,
        branch=task.worktree_branch,
        base_ref=worktree_base_ref,
        detach=task.detached_head,
        allow_destructive_branch_reset=task.allow_destructive_branch_reset,
        protected_head_sha=task.protected_head_sha,
    )
    print(f"[atlas-dispatch] worktree={worktree.worktree_path}")

    mcp_runtime = _prepare_mcp_runtime(
        task.cli, worktree.worktree_path, task.mcp_servers
    )
    requested_model = (
        task.resolved_via.removeprefix("model_registry:")
        if task.resolved_via.startswith("model_registry:")
        else task.model
    )
    invocation = resolve_invocation(
        cli=task.cli,
        cwd=worktree.worktree_path,
        model=task.model,
        reasoning_effort=task.reasoning_effort,
        requested_model=requested_model,
        resolved_via=task.resolved_via,
        prompt=prompt,
    )
    command = invocation.command
    if mcp_runtime is not None and mcp_runtime.config_path is not None:
        command = inject_mcp_config(
            task.cli,
            command,
            mcp_runtime.config_path,
        )

    progress.cli_invoked = True
    # Recorded BEFORE the CLI runs, because it is the only thing that can tell
    # "the agent did nothing" from "the agent's work was absorbed into base_ref".
    # See _should_classify_no_change.
    head_before_cli = _worktree_head_sha(worktree.worktree_path)
    # A worktree shares one .git with the orchestrator, so the agent can move
    # refs the allowlist cannot see. Snapshot them; compare after.
    orchestrator_refs_before = _capture_orchestrator_refs(task.target_repo)

    cli_result = run_cli(
        cli=task.cli,
        prompt=prompt,
        cwd=worktree.worktree_path,
        expected_deliverable_path=_expected_review_deliverable_path(
            task,
            workspace=worktree.worktree_path,
        ),
        model=task.model,
        reasoning_effort=task.reasoning_effort,
        timeout_seconds=task.timeout_seconds,
        idle_timeout_seconds=task.idle_timeout_seconds,
        extra_env=_cli_run_extra_env(run_dir=run_dir, mcp_runtime=mcp_runtime),
        command=command,
    )
    orchestrator_refs_after = _capture_orchestrator_refs(task.target_repo)
    orchestrator_ref_findings = _diff_orchestrator_refs(
        orchestrator_refs_before, orchestrator_refs_after
    )
    orchestrator_ref_guard: dict[str, object] = {
        "schema_version": 2,
        "repo": str(task.target_repo),
        "protected_refs": list(ORCHESTRATOR_PROTECTED_REFS),
        "observed_remote_refs": list(ORCHESTRATOR_OBSERVED_REMOTE_REFS),
        "before": orchestrator_refs_before,
        "after": orchestrator_refs_after,
        "findings": orchestrator_ref_findings,
        "passed": not orchestrator_ref_findings,
        # Compatibility with the first, report-only version of this artifact.
        "clean": not orchestrator_ref_findings,
    }
    _write_json_atomic(
        run_dir / "orchestrator_refs.json",
        orchestrator_ref_guard,
    )
    if orchestrator_ref_findings:
        # Loud on stdout: a detector whose output only lands in a file nobody
        # opens is not detection.
        print(
            "[atlas-dispatch] ORCHESTRATOR REF GUARD FAILED -- "
            "a protected ref moved or could not be read:"
        )
        for finding in orchestrator_ref_findings:
            print(f"[atlas-dispatch]     {finding}")
        print(
            "[atlas-dispatch] Review before trusting this run; see "
            "orchestrator_refs.json."
        )
    else:
        print("[atlas-dispatch] orchestrator refs unchanged during dispatch")
    no_change = _should_classify_no_change(
        worktree=worktree,
        cli_result=cli_result,
        base_ref=worktree_base_ref,
        head_before_cli=head_before_cli,
    )
    if (
        no_change
        and cli_result.classification is not None
        and cli_result.classification.kind == DispatchErrorKind.SUCCESS
    ):
        cli_result = replace(
            cli_result,
            classification=Classification(
                kind=DispatchErrorKind.NO_CHANGE,
                suggested_action=NO_CHANGE_SUGGESTED_ACTION,
            ),
        )
    _persist_cli_result(
        run_dir,
        cli_result,
        task=task,
        review_model_evidence=invocation.review_model_evidence.to_dict(),
        orchestrator_ref_guard=orchestrator_ref_guard,
    )
    classification = cli_result.classification
    classification_str = classification.kind.value if classification else "unknown"
    print(
        f"[atlas-dispatch] {task.cli} exit={cli_result.exit_code} "
        f"duration={cli_result.duration_seconds:.1f}s "
        f"timed_out={cli_result.timed_out} "
        f"classification={classification_str}"
    )
    if classification and classification.kind != DispatchErrorKind.SUCCESS:
        print(f"[atlas-dispatch] suggested action: {classification.suggested_action}")

    auto_commit_message = f"{task.cli}({task.id}): {task.title}"
    auto_committed = (
        False if no_change else commit_all(worktree, message=auto_commit_message)
    )
    print(f"[atlas-dispatch] auto_committed={auto_committed}")

    verified_head_sha = _resolve_verified_head_sha(worktree.worktree_path)
    changed = list_changed_files(worktree, base_ref=worktree_base_ref)
    changed_evidence = _known_changed_files_evidence(changed)
    _write_changed_files_evidence(run_dir, changed_evidence)
    _persist_changed_files_summary(run_dir, changed_evidence)

    cli_succeeded = (
        cli_result.exit_code == 0
        and not cli_result.timed_out
        and (classification is None or classification.kind == DispatchErrorKind.SUCCESS)
    )
    # Publication is preservation, so it happens before post-run verification.
    # A crash or kill during a long acceptance suite must not leave the
    # completed head reachable only from one machine's disk.
    branch_publish = _publish_branch_if_requested(
        run_dir=run_dir,
        task=task,
        worktree=worktree,
        built_sha=verified_head_sha,
        verification_passed=False,
        cli_succeeded=cli_succeeded,
    )

    post_run_heartbeat = _PostRunHeartbeat(
        run_dir=run_dir,
        task=task,
        worktree_path=worktree.worktree_path,
        heartbeat=heartbeat,
    )
    try:
        post_run_started = post_run_heartbeat.start()
        _persist_post_run_started(
            run_dir=run_dir,
            task=task,
            post_run=post_run_started,
        )
        allow_passed, forbidden_violations, out_of_scope = check_allowlist(
            changed_files=changed,
            allowed_paths=task.allowed_paths,
            forbidden_paths=task.forbidden_paths,
        )
        protected_path_violations = check_protected_paths(changed_files=changed)

        command_results: list[CheckResult] = []
        if cli_succeeded and changed:
            if task.acceptance_min_collected is None:
                # Preserve the exact call contract and behavior for every
                # existing spec that declares no collection floor.
                command_results = run_acceptance_commands(
                    commands=task.acceptance,
                    cwd=worktree.worktree_path,
                    timeout_seconds=task.acceptance_timeout_seconds,
                    import_provenance_required=_acceptance_requires_import_provenance(
                        task.acceptance,
                        enabled=task.check_import_provenance,
                    ),
                    run_dir=run_dir,
                )
            else:
                command_results = run_acceptance_commands(
                    commands=task.acceptance,
                    cwd=worktree.worktree_path,
                    timeout_seconds=task.acceptance_timeout_seconds,
                    acceptance_min_collected=task.acceptance_min_collected,
                    import_provenance_required=_acceptance_requires_import_provenance(
                        task.acceptance,
                        enabled=task.check_import_provenance,
                    ),
                    run_dir=run_dir,
                )

        acceptance_skip_reason: list[str] | None = None
        if not command_results:
            acceptance_skip_reason = []
            if not cli_succeeded:
                acceptance_skip_reason.append("cli_failed")
            if not changed:
                acceptance_skip_reason.append("no_files_changed")
            if not allow_passed:
                acceptance_skip_reason.append("allowlist_failed")
            if not task.acceptance:
                # A spec that declares NO acceptance commands was not gated.
                # That is a different fact from "we ran the gate and it failed",
                # and from "something went wrong and we cannot say", so it gets
                # its own reason instead of falling through to "unknown".
                #
                # verify_passed deliberately stays False. Not gated is not
                # passed, and a fallback that turns an absent check into a green
                # one makes every real failure unreadable.
                acceptance_skip_reason.append("no_acceptance_configured")
            if not acceptance_skip_reason:
                acceptance_skip_reason = ["unknown"]

        report = VerifyReport(
            changed_files_present=bool(changed),
            allowlist_passed=allow_passed,
            forbidden_violations=forbidden_violations,
            out_of_scope_paths=out_of_scope,
            protected_path_violations=protected_path_violations,
            command_results=command_results,
            acceptance_skip_reason=acceptance_skip_reason,
        )
        _write_combined_diff_artifact(
            run_dir=run_dir,
            task=task,
            worktree=worktree,
            base_ref=worktree_base_ref,
        )
    except BaseException as exc:
        post_run_incomplete = post_run_heartbeat.finish(
            state="incomplete",
            error=exc,
        )
        _persist_post_run_incomplete(
            run_dir=run_dir,
            task=task,
            post_run=post_run_incomplete,
            error=exc,
        )
        raise

    post_run_completed = post_run_heartbeat.finish(state="completed")
    _persist_verification_summary(
        run_dir,
        report,
        verified_head_sha=verified_head_sha,
        source_base_sha=source_base_sha,
        post_run=post_run_completed,
        ref_guard_passed=not orchestrator_ref_findings,
    )
    _write_report(
        run_dir,
        task=task,
        cli_result=cli_result,
        report=report,
        changed=changed,
        worktree_path=worktree.worktree_path,
        branch_publish=branch_publish,
        post_run=post_run_completed,
        orchestrator_ref_guard=orchestrator_ref_guard,
    )

    verify_passed = report.passed and not orchestrator_ref_findings
    build_ok = cli_succeeded and verify_passed
    publish_ok: bool | str = (
        "not_requested"
        if not task.push_branch
        else branch_publish is not None and branch_publish.pushed
    )
    print(
        f"[atlas-dispatch] verify_passed={verify_passed} "
        f"build_ok={build_ok} publish_ok={publish_ok}"
    )
    print(f"[atlas-dispatch] worktree retained at {worktree.worktree_path}")
    print(f"[atlas-dispatch] branch retained as {worktree.branch}")
    return 0 if build_ok else 1


def _cli_run_extra_env(
    *,
    run_dir: Path,
    mcp_runtime: McpRuntime | None,
) -> dict[str, str]:
    """Return internal overrides for only the dispatched CLI subprocess."""
    extra_env = dict(mcp_runtime.extra_env or {}) if mcp_runtime is not None else {}
    extra_env["UV_CACHE_DIR"] = str((run_dir / "uv-cache").resolve())
    return extra_env


def _worktree_base_ref(
    base_ref: str,
    *,
    remote_name: str,
    target_repo: Path,
) -> str:
    base_ref = base_ref.strip() or "main"
    remote_name = remote_name.strip() or "origin"
    if base_ref == "main" or _base_ref_is_remote_or_direct_ref(base_ref, remote_name=remote_name):
        return base_ref
    if _git_ref_resolves(target_repo, f"refs/heads/{base_ref}"):
        return base_ref
    return f"{remote_name}/{base_ref}"


@contextmanager
def _pre_dispatch_repo_lock(target_repo: Path) -> Iterator[Path]:
    lock_path = _pre_dispatch_repo_lock_path(target_repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _pre_dispatch_in_process_lock(lock_path)
    with process_lock:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield lock_path
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _pre_dispatch_in_process_lock(lock_path: Path) -> threading.Lock:
    with _PREDISPATCH_REPO_LOCKS_GUARD:
        lock = _PREDISPATCH_REPO_LOCKS.get(lock_path)
        if lock is None:
            lock = threading.Lock()
            _PREDISPATCH_REPO_LOCKS[lock_path] = lock
        return lock


def _pre_dispatch_repo_lock_path(target_repo: Path) -> Path:
    repo_root = target_repo.resolve()
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return dot_git / "atlas-dispatch-pre-dispatch-sync.lock"
    return repo_root.parent / f".{repo_root.name}.atlas-dispatch-pre-dispatch-sync.lock"


def _pre_dispatch_git_lock_contention_error(
    *,
    target_repo: Path,
    base_ref: str,
    action: str,
    result: subprocess.CompletedProcess[str],
) -> PreDispatchGitSyncError:
    return PreDispatchGitSyncError(
        f"Transient pre-dispatch git index lock contention "
        f"({PREDISPATCH_GIT_LOCK_CONTENTION_KIND}) while trying to {action} "
        f"{base_ref} on {target_repo}; retry or redispatch. "
        f"Git output: {_git_result_error(result)}",
        classification=PREDISPATCH_GIT_LOCK_CONTENTION_KIND,
        transient=True,
        suggested_action=PREDISPATCH_GIT_LOCK_CONTENTION_SUGGESTED_ACTION,
    )


def _raise_if_pre_dispatch_git_lock_contention(
    *,
    target_repo: Path,
    base_ref: str,
    action: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    if git_result_is_index_lock_contention(result):
        raise _pre_dispatch_git_lock_contention_error(
            target_repo=target_repo,
            base_ref=base_ref,
            action=action,
            result=result,
        )


def _run_pre_dispatch_git(
    repo: Path,
    args: list[str],
    *,
    sleep_fn: Callable[[float], None],
) -> subprocess.CompletedProcess[str]:
    for attempt_index in range(
        len(PREDISPATCH_GIT_INDEX_LOCK_RETRY_DELAYS_SECONDS) + 1
    ):
        result = _run_git(repo, args)
        if not git_result_is_index_lock_contention(result):
            return result
        if attempt_index >= len(PREDISPATCH_GIT_INDEX_LOCK_RETRY_DELAYS_SECONDS):
            return result
        sleep_fn(PREDISPATCH_GIT_INDEX_LOCK_RETRY_DELAYS_SECONDS[attempt_index])
    return result


def _run_pre_dispatch_fetch(
    git: Callable[[list[str]], subprocess.CompletedProcess[str]],
    args: list[str],
    *,
    sleep_fn: Callable[[float], None],
) -> subprocess.CompletedProcess[str]:
    for attempt_index in range(
        len(PREDISPATCH_GIT_FETCH_CONNECTION_RETRY_DELAYS_SECONDS) + 1
    ):
        result = git(args)
        if not _git_result_is_transient_connection_error(result):
            return result
        if attempt_index >= len(
            PREDISPATCH_GIT_FETCH_CONNECTION_RETRY_DELAYS_SECONDS
        ):
            return result
        sleep_fn(
            PREDISPATCH_GIT_FETCH_CONNECTION_RETRY_DELAYS_SECONDS[attempt_index]
        )
    return result


def _pre_dispatch_sync_base_ref(
    *,
    target_repo: Path,
    base_ref: str,
    remote_name: str = "origin",
    sleep_fn: Callable[[float], None] | None = None,
) -> str:
    """Fast-forward a local base branch before dispatch creates a worktree."""

    base_ref = base_ref.strip() or "main"
    remote_name = remote_name.strip() or "origin"

    if not target_repo.is_dir() or not (target_repo / ".git").exists():
        return base_ref

    if _base_ref_is_remote_or_direct_ref(base_ref, remote_name=remote_name):
        return base_ref

    sleep = time.sleep if sleep_fn is None else sleep_fn

    def git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return _run_pre_dispatch_git(target_repo, args, sleep_fn=sleep)

    def fetch_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return _run_pre_dispatch_fetch(git, args, sleep_fn=sleep)

    with _pre_dispatch_repo_lock(target_repo):
        return _pre_dispatch_sync_base_ref_locked(
            target_repo=target_repo,
            base_ref=base_ref,
            remote_name=remote_name,
            git=git,
            fetch_git=fetch_git,
        )


def _pre_dispatch_sync_base_ref_locked(
    *,
    target_repo: Path,
    base_ref: str,
    remote_name: str,
    git: Callable[[list[str]], subprocess.CompletedProcess[str]],
    fetch_git: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> str:
    if base_ref != "main":
        resolved_base_ref = _worktree_base_ref(
            base_ref,
            remote_name=remote_name,
            target_repo=target_repo,
        )
        if resolved_base_ref == base_ref:
            return resolved_base_ref
        fetch_refspec = f"{base_ref}:refs/remotes/{remote_name}/{base_ref}"
        fetch = fetch_git(["fetch", remote_name, fetch_refspec])
        if fetch.returncode != 0:
            if _git_result_is_missing_remote(fetch, remote_name):
                return resolved_base_ref
            _raise_if_pre_dispatch_git_lock_contention(
                target_repo=target_repo,
                base_ref=base_ref,
                action=f"fetch {remote_name}/{base_ref}",
                result=fetch,
            )
            raise PreDispatchGitSyncError(
                f"Could not fetch {remote_name}/{base_ref} for {target_repo} before "
                f"dispatch; manual intervention required: {_git_result_error(fetch)}"
            )
        remote_rev = git(["rev-parse", resolved_base_ref])
        if remote_rev.returncode != 0:
            _raise_if_pre_dispatch_git_lock_contention(
                target_repo=target_repo,
                base_ref=base_ref,
                action=f"resolve {resolved_base_ref}",
                result=remote_rev,
            )
            raise PreDispatchGitSyncError(
                f"Could not resolve {resolved_base_ref} after fetch on {target_repo}; "
                f"manual intervention required: {_git_result_error(remote_rev)}"
            )
        return resolved_base_ref

    checkout = git(["checkout", base_ref])
    if checkout.returncode != 0:
        _raise_if_pre_dispatch_git_lock_contention(
            target_repo=target_repo,
            base_ref=base_ref,
            action="checkout local",
            result=checkout,
        )
        raise PreDispatchGitSyncError(
            f"Could not checkout local {base_ref} on {target_repo} before "
            f"dispatch; manual intervention required: {_git_result_error(checkout)}"
        )

    fetch = fetch_git(["fetch", remote_name, base_ref])
    if fetch.returncode != 0:
        if _git_result_is_missing_remote(fetch, remote_name):
            return base_ref
        _raise_if_pre_dispatch_git_lock_contention(
            target_repo=target_repo,
            base_ref=base_ref,
            action=f"fetch {remote_name}/{base_ref}",
            result=fetch,
        )
        raise PreDispatchGitSyncError(
            f"Could not fetch {remote_name}/{base_ref} for {target_repo} before "
            f"dispatch; manual intervention required: {_git_result_error(fetch)}"
        )

    remote_ref = f"{remote_name}/{base_ref}"
    remote_rev = git(["rev-parse", remote_ref])
    if remote_rev.returncode != 0:
        _raise_if_pre_dispatch_git_lock_contention(
            target_repo=target_repo,
            base_ref=base_ref,
            action=f"resolve {remote_ref}",
            result=remote_rev,
        )
        raise PreDispatchGitSyncError(
            f"Could not resolve {remote_ref} after fetch on {target_repo}; "
            f"manual intervention required: {_git_result_error(remote_rev)}"
        )

    head_rev = git(["rev-parse", "HEAD"])
    if head_rev.returncode != 0:
        _raise_if_pre_dispatch_git_lock_contention(
            target_repo=target_repo,
            base_ref=base_ref,
            action="resolve HEAD",
            result=head_rev,
        )
        raise PreDispatchGitSyncError(
            f"Could not resolve HEAD on {target_repo} before dispatch; "
            f"manual intervention required: {_git_result_error(head_rev)}"
        )

    if head_rev.stdout.strip() == remote_rev.stdout.strip():
        return base_ref

    merge = git(["merge", "--ff-only", remote_ref])
    if merge.returncode == 0:
        return base_ref
    _raise_if_pre_dispatch_git_lock_contention(
        target_repo=target_repo,
        base_ref=base_ref,
        action=f"fast-forward from {remote_ref}",
        result=merge,
    )

    rev_list = git(["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"])
    try:
        ahead_str, behind_str = rev_list.stdout.strip().split("\t")
        ahead = int(ahead_str)
        behind = int(behind_str)
    except Exception:
        ahead = behind = 0

    if ahead > 0 and behind > 0:
        raise PreDispatchGitSyncError(
            f"Local {base_ref} has DIVERGED from {remote_ref}: ahead={ahead}, "
            f"behind={behind}. Manual reconciliation required before dispatch."
        )
    if ahead > 0:
        raise PreDispatchGitSyncError(
            f"Local {base_ref} is AHEAD of {remote_ref} by {ahead} commit(s); "
            "manual intervention required before dispatch."
        )
    if behind > 0:
        raise PreDispatchGitSyncError(
            f"Local {base_ref} is behind {remote_ref} by {behind} commit(s), "
            f"but fast-forward failed. Run: git -C {target_repo} merge --ff-only {remote_ref}"
        )
    raise PreDispatchGitSyncError(
        f"Local {base_ref} diverged from {remote_ref} on {target_repo}; "
        f"manual intervention required: {_git_result_error(merge)}"
    )


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return run_git(args, cwd=repo, check=False)


def _combined_diff_git(
    worktree_path: Path, args: list[str]
) -> subprocess.CompletedProcess[str]:
    return _run_git(worktree_path, args)


def _resolve_verified_head_sha(worktree_path: Path) -> str:
    result = _run_git(worktree_path, ["rev-parse", "--verify", "HEAD^{commit}"])
    head_sha = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None:
        detail = result.stderr.strip() or head_sha or f"exit {result.returncode}"
        raise RuntimeError(f"could not resolve verified worktree HEAD: {detail}")
    return head_sha


def _write_combined_diff_artifact(
    *,
    run_dir: Path,
    task: TaskSpec,
    worktree: Worktree,
    base_ref: str,
) -> Path:
    short_sha = "unknown"
    content: str
    try:
        head_result = _combined_diff_git(worktree.worktree_path, ["rev-parse", "HEAD"])
        short_result = _combined_diff_git(
            worktree.worktree_path, ["rev-parse", "--short", "HEAD"]
        )

        head_ref = "HEAD"
        errors: list[str] = []
        if head_result.returncode == 0 and head_result.stdout.strip():
            head_ref = head_result.stdout.strip()
        else:
            errors.append(f"git rev-parse HEAD failed: {_git_result_error(head_result)}")
        if short_result.returncode == 0 and short_result.stdout.strip():
            short_sha = short_result.stdout.strip()
        else:
            errors.append(
                f"git rev-parse --short HEAD failed: {_git_result_error(short_result)}"
            )

        diff_args = ["diff", f"{base_ref}...{head_ref}"]
        diff_result = _combined_diff_git(worktree.worktree_path, diff_args)
        if diff_result.returncode == 0:
            content = diff_result.stdout
        else:
            errors.append(
                f"git {' '.join(diff_args)} failed: {_git_result_error(diff_result)}"
            )
            content = _combined_diff_error_stub(
                worktree_path=worktree.worktree_path,
                command=["git", *diff_args],
                errors=errors,
                stdout=diff_result.stdout,
                stderr=diff_result.stderr,
            )
            LOGGER.warning(
                "combined diff artifact generation failed for task %s: %s",
                task.id,
                errors[-1],
            )
    except Exception as exc:  # pragma: no cover - defensive artifact isolation
        content = _combined_diff_error_stub(
            worktree_path=worktree.worktree_path,
            command=["git", "diff", f"{base_ref}...HEAD"],
            errors=[f"{type(exc).__name__}: {exc}"],
            stdout="",
            stderr="",
        )
        LOGGER.warning(
            "combined diff artifact generation raised for task %s: %s",
            task.id,
            exc,
        )

    artifact_path = run_dir / f"combined-{short_sha}.diff"
    _write_text_artifact(artifact_path, content)
    return artifact_path


def _combined_diff_error_stub(
    *,
    worktree_path: Path,
    command: list[str],
    errors: list[str],
    stdout: str,
    stderr: str,
) -> str:
    lines = [
        "Combined diff artifact generation failed.",
        "",
        f"Worktree: {worktree_path}",
        f"Command: {' '.join(command)}",
        "",
        "Errors:",
        *[f"- {error}" for error in errors],
    ]
    if stdout.strip():
        lines.extend(["", "Stdout:", stdout.strip()])
    if stderr.strip():
        lines.extend(["", "Stderr:", stderr.strip()])
    return "\n".join(lines) + "\n"


def _write_text_artifact(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("could not write artifact %s: %s", path, exc)


def _base_ref_is_remote_or_direct_ref(base_ref: str, *, remote_name: str) -> bool:
    if base_ref.startswith("refs/"):
        return True
    if base_ref.startswith(f"{remote_name}/"):
        return True
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", base_ref))


def _git_ref_resolves(target_repo: Path, ref: str) -> bool:
    result = _run_git(target_repo, ["rev-parse", "--verify", ref])
    return result.returncode == 0


def _git_result_is_missing_remote(
    result: subprocess.CompletedProcess[str], remote_name: str
) -> bool:
    output = f"{result.stderr}\n{result.stdout}"
    return (
        f"No such remote '{remote_name}'" in output
        or f"No such remote: {remote_name}" in output
        or f"'{remote_name}' does not appear to be a git repository" in output
    )


def _git_result_is_transient_connection_error(
    result: subprocess.CompletedProcess[str],
) -> bool:
    stderr = (result.stderr or "").casefold()
    return result.returncode != 0 and any(
        marker in stderr
        for marker in (
            "connection refused",
            "econnrefused",
            "ehostunreach",
            "could not resolve host",
            "failed to connect",
        )
    )


def _git_result_error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"git exited {result.returncode}").strip()


def _should_classify_no_change(
    *,
    worktree: Worktree,
    cli_result: AdapterResult,
    base_ref: str,
    head_before_cli: str | None = None,
) -> bool:
    """Did this run actually produce nothing?

    `base_ref..HEAD == 0` DOES NOT MEAN THAT. If an implementer builds, commits,
    and then merges its own branch into base_ref (or base_ref simply moves ahead
    underneath a long run), HEAD becomes an ancestor of base_ref and the count is
    legitimately zero. Reading that as `no_change` suggests a retry, and a retry
    force-resets the branch: the advice would destroy completed work.

    Two different things collapse into that zero:
      - the agent did nothing                      -> genuinely no_change
      - the agent's commits were absorbed into base_ref, by its own merge or by
        base_ref moving ahead underneath a long run -> emphatically NOT no_change

    So the question is whether HEAD MOVED, which is a fact about this run rather
    than about the relationship between two refs that other people are pushing to.
    `head_before_cli` is recorded before the CLI is invoked. The base_ref check is
    kept as a fallback for callers that cannot supply it, and is the older, weaker
    predicate.
    """
    if cli_result.exit_code != 0 or cli_result.timed_out:
        return False

    if _has_uncommitted_work(worktree.worktree_path):
        return False

    if head_before_cli:
        head_after = _worktree_head_sha(worktree.worktree_path)
        if head_after is None:
            # Cannot tell. Refusing to claim no_change is the safe direction:
            # the cost of a wrong no_change is a destructive retry.
            return False
        return head_after == head_before_cli

    ahead_count = _commits_ahead_of_base(
        worktree.worktree_path,
        base_ref=base_ref,
    )
    if ahead_count is None or ahead_count != 0:
        return False

    return True


ORCHESTRATOR_PROTECTED_REFS: tuple[str, ...] = ("main", "HEAD")
_ORCHESTRATOR_REF_REVISIONS = {
    "main": "refs/heads/main",
    "HEAD": "HEAD",
}
_ORCHESTRATOR_REMOTE_REF_REVISIONS = {
    "origin/main": "refs/heads/main",
}
ORCHESTRATOR_OBSERVED_REMOTE_REFS: tuple[str, ...] = tuple(
    _ORCHESTRATOR_REMOTE_REF_REVISIONS
)


def _capture_orchestrator_refs(repo: Path) -> dict[str, Any]:
    """Snapshot the refs a dispatched agent must never move.

    A worktree is a FILESYSTEM boundary, not a git-ref boundary: every worktree
    shares one ``.git``, so an agent running with full-access flags can move the
    orchestrator's ``main`` -- and ``allowed_paths`` cannot see it, because no
    file in the orchestrator's checkout changed. See ADR-003 in
    docs/DESIGN_DECISIONS.md.

    Capture failures are recorded in the snapshots. Unreadable local protected
    refs fail closed; an unavailable remote observation is surfaced without
    making network availability a prerequisite for local dispatches. A change
    from resolved to unresolved (or the reverse) is still a mismatch.
    """
    refs: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    for ref in ORCHESTRATOR_PROTECTED_REFS:
        revision = _ORCHESTRATOR_REF_REVISIONS[ref]
        result = _run_git(
            repo,
            ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        )
        sha = result.stdout.strip()
        if result.returncode == 0 and sha:
            refs[ref] = sha
        elif (
            ref != "HEAD"
            and result.returncode == 1
            and not result.stdout.strip()
            and not result.stderr.strip()
        ):
            # `rev-parse --verify --quiet` exits 1 with no output when the ref
            # does not exist. That is a readable fact (a repository whose base
            # branch is not called `main`), not an unreadable ref. It is still
            # compared across snapshots, so the branch appearing or
            # disappearing during the run is a finding.
            refs[ref] = None
        else:
            refs[ref] = None
            errors[ref] = " ".join(
                (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"git rev-parse exited {result.returncode}"
                ).split()
            )[:200]

    for remote_ref, advertised_ref in _ORCHESTRATOR_REMOTE_REF_REVISIONS.items():
        result = _run_git(repo, ["ls-remote", "--refs", "origin", advertised_ref])
        if result.returncode == 0:
            advertised = [line.split() for line in result.stdout.splitlines()]
            refs[remote_ref] = next(
                (
                    parts[0]
                    for parts in advertised
                    if len(parts) == 2 and parts[1] == advertised_ref
                ),
                None,
            )
        else:
            refs[remote_ref] = None
            errors[remote_ref] = " ".join(
                (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"git ls-remote exited {result.returncode}"
                ).split()
            )[:200]

    return {"refs": refs, "errors": errors}


def _diff_orchestrator_refs(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[str]:
    """Name every moved ref and every unreadable local protected ref."""
    findings: list[str] = []
    before_refs = dict(before.get("refs") or {})
    after_refs = dict(after.get("refs") or {})
    for ref in (*ORCHESTRATOR_PROTECTED_REFS, *ORCHESTRATOR_OBSERVED_REMOTE_REFS):
        was, now = before_refs.get(ref), after_refs.get(ref)
        if was != now:
            # SAY WHAT IS OBSERVED, NOT WHO DID IT. This guard compares two
            # snapshots; it cannot distinguish the dispatched agent from the
            # orchestrator committing on main during the run, and in practice
            # both happen. A finding that names the wrong culprit is how a
            # correct alarm gets dismissed.
            findings.append(
                f"{ref} moved during this run: {was or 'unresolved'} "
                f"-> {now or 'unresolved'} (the dispatched agent or the "
                f"orchestrator; a worktree isolates files, not refs)"
            )
    for label, snapshot in (("before", before), ("after", after)):
        errors = dict(snapshot.get("errors") or {})
        for ref in ORCHESTRATOR_PROTECTED_REFS:
            if error := errors.get(ref):
                findings.append(f"could not record {ref} {label} dispatch: {error}")
    return findings


def _worktree_head_sha(worktree_path: Path) -> str | None:
    result = _run_git(worktree_path, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _commits_ahead_of_base(worktree_path: Path, *, base_ref: str) -> int | None:
    result = _run_git(worktree_path, ["rev-list", "--count", f"{base_ref}..HEAD"])
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _has_uncommitted_work(worktree_path: Path) -> bool:
    result = _run_git(worktree_path, ["status", "--porcelain"])
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _persist_cli_result(
    run_dir: Path,
    result: AdapterResult,
    *,
    task: TaskSpec | None = None,
    review_model_evidence: Mapping[str, object] | None = None,
    orchestrator_ref_guard: Mapping[str, object] | None = None,
) -> None:
    (run_dir / "cli.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "cli.stderr.txt").write_text(result.stderr, encoding="utf-8")
    classification_payload: dict[str, Any] | None = None
    if result.classification is not None:
        classification_payload = {
            "kind": result.classification.kind.value,
            "suggested_action": result.classification.suggested_action,
            "matched_pattern": result.classification.matched_pattern,
        }
        if result.classification.quota_reset_window is not None:
            classification_payload["quota_reset_window"] = (
                result.classification.quota_reset_window
            )
        if result.classification.quota_reset_window_provenance is not None:
            classification_payload["quota_reset_window_provenance"] = (
                result.classification.quota_reset_window_provenance.value
            )
    summary: dict[str, Any] = {
        "cli": result.cli,
        "exit_code": result.exit_code,
        "duration_seconds": round(result.duration_seconds, 2),
        "timed_out": result.timed_out,
        "command": _sanitize_command_for_artifact(result.command),
        "classification": classification_payload,
    }
    if review_model_evidence is not None:
        summary["review_model_evidence"] = dict(review_model_evidence)
    if orchestrator_ref_guard is not None:
        summary["orchestrator_ref_guard"] = dict(orchestrator_ref_guard)
    if _has_codex_lifecycle_summary(result):
        summary["turn_lifecycle"] = result.turn_lifecycle or []
        summary["final_turn_status"] = result.final_turn_status
        if result.token_usage is not None:
            summary["token_usage"] = result.token_usage
        if result.error_info is not None:
            summary["error_info"] = result.error_info
        summary["idle_classification"] = result.idle_classification
    if result.run_identity is not None:
        summary["run_identity"] = result.run_identity
        (run_dir / "run-identity.json").write_text(
            json.dumps(result.run_identity, indent=2), encoding="utf-8"
        )
    if task is None:
        # Artifact-only callers persist the CLI observation and do not claim to
        # have entered the dispatcher's post-run lifecycle.
        _write_json_atomic(run_dir / "cli.summary.json", summary)
        _write_text_atomic(
            run_dir / "report.md",
            "\n".join(
                [
                    "# CLI result artifact",
                    "",
                    "This helper call persisted CLI output only; it did not run "
                    "post-run verification.",
                    "",
                    f"- CLI: `{result.cli}`",
                    f"- CLI exit: `{result.exit_code}`",
                ]
            ),
        )
        return
    summary["cli_invoked"] = True
    summary["reuse_policy"] = task.reuse_policy
    summary["run_reuse"] = {
        "reused": False,
        "kind": (
            "explicit_fresh_run"
            if task.reuse_policy == REUSE_POLICY_NEVER
            else "new_run"
        ),
        "cli_invoked": True,
    }
    summary.update(
        {
            "classification": {
                "kind": POST_RUN_INCOMPLETE_CLASSIFICATION,
                "suggested_action": (
                    "Preserve the published branch and inspect post_run state; "
                    "never treat this as an acceptance failure or redispatch it."
                ),
                "matched_pattern": None,
            },
            "cli_classification": classification_payload,
            # These fields are deliberately non-null and non-verifying. The
            # unsupported state makes legacy projection fail closed instead of
            # manufacturing VERIFIED_FAIL from an absent verdict.
            "verify_passed": False,
            "acceptance_failed": False,
            "acceptance_outcome": "skipped",
            "verification_state": POST_RUN_INCOMPLETE_VERIFICATION_STATE,
            "verified_head_sha": "",
            "acceptance_commands": [],
            "acceptance_skip_reason": [POST_RUN_INCOMPLETE_CLASSIFICATION],
            "post_run": {
                "state": "not_started",
                "classification": POST_RUN_INCOMPLETE_CLASSIFICATION,
                "started_at": "not_recorded",
                "last_heartbeat_at": "not_recorded",
                "ended_at": "not_recorded",
                "duration_seconds": 0.0,
                "supervising_timeout_seconds": task.timeout_seconds,
                "acceptance_timeout_seconds": task.acceptance_timeout_seconds,
                "heartbeat_interval_seconds": POST_RUN_HEARTBEAT_INTERVAL_SECONDS,
                "heartbeat_artifact": str(
                    (run_dir / POST_RUN_HEARTBEAT_ARTIFACT).resolve()
                ),
            },
        }
    )
    # This first-phase record is not a verification verdict. Keep the marker
    # until _persist_verification_summary durably writes the post-run fields.
    summary[POST_RUN_INCOMPLETE_MARKER] = True
    _write_json_atomic(run_dir / "cli.summary.json", summary)
    _write_post_run_report(run_dir=run_dir, task=task, summary=summary)


def _write_post_run_report(
    *,
    run_dir: Path,
    task: TaskSpec,
    summary: Mapping[str, object],
) -> None:
    """Render a snapshot while ``cli.summary.json`` remains authoritative."""

    post_run = summary.get("post_run")
    post_run = post_run if isinstance(post_run, Mapping) else {}
    state = str(post_run.get("state") or "unknown")
    title = (
        "# POST-RUN IN PROGRESS"
        if state == "running"
        else "# INCOMPLETE RUN: post-run phase did not complete"
    )
    changed = _read_changed_files_evidence(run_dir / "changed_files.txt")
    branch_publish_path = run_dir / "branch_publish.json"
    try:
        branch_publish = json.loads(branch_publish_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        branch_publish = {}
    pushed = (
        branch_publish.get("pushed")
        if isinstance(branch_publish, dict)
        else "not_recorded"
    )
    lines = [
        title,
        "",
        "**This is not an acceptance verdict and must not be classified as a "
        "verification failure.**",
        "",
        "`cli.summary.json` is authoritative for verdict state. This report is a "
        "timestamped snapshot and is rewritten on normal completion; use "
        "`post_run.json` heartbeat age to distinguish live work from a dead run.",
        "",
        f"- Task: `{task.id}`",
        f"- CLI: `{summary.get('cli')}`",
        f"- CLI exit: `{summary.get('exit_code')}` "
        f"(timed_out={summary.get('timed_out')})",
        f"- CLI duration: {summary.get('duration_seconds')}s",
        f"- Classification: **{POST_RUN_INCOMPLETE_CLASSIFICATION}**",
        f"- Post-run state: **{state.upper()}**",
        f"- Post-run started: `{post_run.get('started_at', 'not_recorded')}`",
        f"- Last heartbeat: `{post_run.get('last_heartbeat_at', 'not_recorded')}`",
        f"- Post-run ended: `{post_run.get('ended_at', 'not_recorded')}`",
        f"- Post-run duration: {post_run.get('duration_seconds', 0.0)}s",
        "- Supervising timeout: "
        f"{post_run.get('supervising_timeout_seconds', 'not_recorded')}s",
        "- Acceptance timeout: "
        f"{post_run.get('acceptance_timeout_seconds', 'not_recorded')}s",
        "",
        "## Changed files",
        "",
        *_changed_files_report_lines(changed),
        "",
        "## Branch publication",
        "",
        f"- Branch: `{task.remote_name}/{task.worktree_branch}`",
        f"- Pushed: **{pushed}**",
        "",
        "## Acceptance commands",
        "",
        "- Acceptance outcome: **SKIPPED (POST-RUN INCOMPLETE)**",
        "- Acceptance did not produce a verdict.",
        *_orchestrator_ref_report_lines(
            summary.get("orchestrator_ref_guard")
            if isinstance(summary.get("orchestrator_ref_guard"), Mapping)
            else None
        ),
        "## Overall",
        "",
        f"- Post-run phase: **{state.upper()}**",
        "- Verification state: **POST_RUN_INCOMPLETE**",
        "- Verify passed: **False (NOT A VERDICT)**",
    ]
    _write_text_atomic(run_dir / "report.md", "\n".join(lines))


def _persist_changed_files_summary(
    run_dir: Path,
    evidence: ChangedFilesEvidence,
) -> None:
    summary_path = run_dir / "cli.summary.json"
    try:
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {}
    summary = raw if isinstance(raw, dict) else {}
    _set_changed_files_summary(summary, evidence)
    _write_json_atomic(summary_path, summary)


def _persist_post_run_started(
    *,
    run_dir: Path,
    task: TaskSpec,
    post_run: Mapping[str, object],
) -> None:
    summary_path = run_dir / "cli.summary.json"
    raw = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("cli.summary.json ceased to be an object before post-run")
    raw["post_run"] = dict(post_run)
    _write_json_atomic(summary_path, raw)
    _write_post_run_report(run_dir=run_dir, task=task, summary=raw)


def _persist_post_run_incomplete(
    *,
    run_dir: Path,
    task: TaskSpec,
    post_run: Mapping[str, object],
    error: BaseException,
) -> None:
    summary_path = run_dir / "cli.summary.json"
    try:
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {}
    summary = raw if isinstance(raw, dict) else {}
    summary.update(
        {
            "classification": {
                "kind": POST_RUN_INCOMPLETE_CLASSIFICATION,
                "suggested_action": (
                    "The branch was preserved before post-run. Re-run acceptance "
                    "against that existing head; do not redispatch."
                ),
                "matched_pattern": None,
            },
            POST_RUN_INCOMPLETE_MARKER: True,
            "verify_passed": False,
            "acceptance_failed": False,
            "acceptance_outcome": "skipped",
            "verification_state": POST_RUN_INCOMPLETE_VERIFICATION_STATE,
            "acceptance_commands": [],
            "acceptance_skip_reason": [POST_RUN_INCOMPLETE_CLASSIFICATION],
            "post_run": dict(post_run),
            "post_run_error": f"{type(error).__name__}: {error}",
        }
    )
    _write_json_atomic(summary_path, summary)
    _write_post_run_report(run_dir=run_dir, task=task, summary=summary)


def _sanitize_command_for_artifact(command: list[str]) -> list[str]:
    """Redact credential-bearing argv values before artifact persistence."""
    secret_name = re.compile(
        r"(?i)(?:api[-_]?key|authorization|bearer|password|secret|token)"
    )
    sanitized: list[str] = []
    redact_next = False
    for token in command:
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if "=" in token:
            name, _separator, _value = token.partition("=")
            sanitized.append(
                f"{name}=<redacted>" if secret_name.search(name) else token
            )
            continue
        sanitized.append(token)
        if token.startswith("-") and secret_name.search(token):
            redact_next = True
    return sanitized


def _persist_verification_summary(
    run_dir: Path,
    report: VerifyReport,
    *,
    verified_head_sha: str,
    source_base_sha: str | None,
    post_run: Mapping[str, object],
    ref_guard_passed: bool = True,
) -> None:
    summary_path = run_dir / "cli.summary.json"
    try:
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {}
    summary = raw if isinstance(raw, dict) else {}
    summary.update(
        _verification_summary(
            report,
            verified_head_sha=verified_head_sha,
            ref_guard_passed=ref_guard_passed,
        )
    )
    cli_classification = summary.get("cli_classification")
    summary["classification"] = (
        cli_classification if isinstance(cli_classification, dict) else None
    )
    summary["post_run"] = dict(post_run)
    # Completion replaces the deliberately provisional POST_RUN_INCOMPLETE
    # state; the completed verdict is carried by verify_passed and
    # acceptance_outcome from here on.
    summary.pop("verification_state", None)
    summary.pop(POST_RUN_INCOMPLETE_MARKER, None)
    if source_base_sha is not None:
        summary["source_base_sha"] = source_base_sha
    _write_json_atomic(summary_path, summary)


def _verification_summary(
    report: VerifyReport,
    *,
    verified_head_sha: str,
    ref_guard_passed: bool = True,
) -> dict[str, Any]:
    return {
        # Do NOT add a `verification_state` key here. That name is a closed
        # vocabulary (verify.VerificationState) whose parser rejects anything
        # else by design, so an unknown value can never read as truthy. It is
        # also popped on post-run completion. The machine-readable truth about
        # "could not check" is carried by acceptance_outcome,
        # acceptance_skip_reason and acceptance_not_attempted_reason below; the
        # human headline is rendered in _write_report.
        "verify_passed": report.passed and ref_guard_passed,
        "acceptance_failed": report.acceptance_outcome != "passed",
        "acceptance_outcome": report.acceptance_outcome,
        "verified_head_sha": verified_head_sha,
        "acceptance_commands": _acceptance_command_summary(report.command_results),
        "acceptance_skip_reason": report.acceptance_skip_reason,
        "acceptance_not_attempted_reason": report.acceptance_not_attempted_reason,
        "protected_path_violations": report.protected_path_violations,
    }


def _acceptance_command_summary(command_results: list[CheckResult]) -> list[dict[str, Any]]:
    exit_codes = getattr(command_results, "exit_codes", None)
    if not isinstance(exit_codes, list):
        exit_codes = []
    commands: list[dict[str, Any]] = []
    for index, check in enumerate(command_results):
        commands.append(
            {
                "name": check.name,
                "passed": check.passed,
                "exit_code": (
                    exit_codes[index]
                    if index < len(exit_codes)
                    else _fallback_acceptance_exit_code(check)
                ),
                "classification": check.classification,
                "pytest_counts": (
                    dict(check.pytest_counts)
                    if isinstance(check.pytest_counts, dict)
                    else check.pytest_counts
                ),
                "acceptance_min_collected": (
                    check.acceptance_min_collected
                    if check.acceptance_min_collected is not None
                    else "not_declared"
                ),
                "duration_seconds": round(check.duration_seconds, 2),
            }
        )
    return commands


def _fallback_acceptance_exit_code(check: CheckResult) -> int | None:
    if check.passed:
        return 0
    if check.classification == "acceptance_timeout":
        return None
    return 1


def _has_codex_lifecycle_summary(result: AdapterResult) -> bool:
    return result.cli.lower() == "codex" and (
        result.turn_lifecycle is not None
        or result.final_turn_status is not None
        or result.token_usage is not None
        or result.error_info is not None
        or result.idle_classification is not None
    )


def _publish_branch_if_requested(
    *,
    run_dir: Path,
    task: TaskSpec,
    worktree: Any,
    verification_passed: bool,
    cli_succeeded: bool,
    built_sha: str = "",
) -> BranchPublishResult | None:
    if not task.push_branch:
        return None

    # PUBLICATION IS PRESERVATION, NOT APPROVAL. Do not gate this on
    # verification. Refusing to push a failed run inverts the risk: the work most
    # likely to be lost (red, partial, needs another look) is exactly the work
    # that would not be preserved. It would also block cross-model review, since
    # a reviewer task uses base_ref=origin/<branch> and cannot start from a
    # branch that is not on the remote.
    #
    # Pushing a branch is not merging it; the harness never merges.
    #
    # A PUSHED BRANCH IS NOT A VERIFIED ONE. verification_passed and
    # cli_succeeded stay in the run report and cli.summary.json; never read
    # "it's on the remote" as "it works".
    result = publish_branch(
        worktree,
        remote_name=task.remote_name,
        built_sha=built_sha,
    )

    _persist_branch_publish(run_dir, result)
    status = "pushed" if result.pushed else "failed"
    print(f"[atlas-dispatch] branch_publish={status} remote_ref={result.remote_ref}")
    if result.error:
        print(f"[atlas-dispatch] branch_publish_error={result.error}")
    return result


def _persist_branch_publish(run_dir: Path, result: BranchPublishResult) -> None:
    payload = {
        "remote_name": result.remote_name,
        "requested_remote_ref": result.requested_remote_ref,
        "remote_ref": result.remote_ref,
        "pushed": result.pushed,
        "state": result.state,
        "built_sha": result.built_sha,
        "remote_head_sha": result.remote_head_sha,
        "verification_state": result.verification_state,
        "verification_detail": result.verification_detail,
        "reason": result.reason,
        "return_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error": result.error,
    }
    _write_json_atomic(run_dir / "branch_publish.json", payload)


def _context_timeout_report_lines(
    run_dir: Path,
) -> list[str]:
    markers = [
        line
        for line in (run_dir / "prompt.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("(context file skipped: read timed out")
    ]
    return [
        "",
        "## Context files",
        "",
        *[f"- **SKIPPED:** {marker}" for marker in markers],
    ] if markers else []


# A BOOLEAN CANNOT CARRY THREE FACTS. `verify_passed=False` conflates "the code
# failed verification" with "verification never happened", and whoever reads it
# decides whether to redispatch. The default reading of False is "it failed,
# start over", which would discard a complete, coherent change whose acceptance
# suite merely never ran (for example, the run hit its timeout mid-suite).
#
# The boolean is unchanged for machine consumers that already gate on it. What
# changes is that the report never states it without saying which fact it means.
VERIFY_STATE_PASS = "VERIFIED_PASS"
VERIFY_STATE_FAIL = "VERIFIED_FAIL"
VERIFY_STATE_NOT_ATTEMPTED = "NOT_ATTEMPTED"


def verification_verdict(
    report: "VerifyReport", *, ref_guard_passed: bool
) -> tuple[str, str]:
    """Return (state, one-line reason) for a run's acceptance verification.

    NOT_ATTEMPTED is deliberately NOT merged into VERIFIED_FAIL. A run whose
    acceptance never executed has produced no evidence about the code either way,
    and saying so is the whole point.
    """
    outcome = report.acceptance_outcome
    reasons = ", ".join(str(r) for r in (report.acceptance_skip_reason or [])) or None

    if outcome in ("skipped", "not_attempted"):
        detail = f" ({reasons})" if reasons else ""
        if not report.changed_files_present and not ref_guard_passed:
            # An empty diff next to a moved ref is NOT evidence that nothing was
            # built: an agent that merged its own branch into base_ref leaves
            # `base_ref...HEAD` empty. Say so instead of claiming a no-op.
            return (
                VERIFY_STATE_NOT_ATTEMPTED,
                f"the diff against the base is empty{detail}, but a ref outside "
                "the worktree moved, so the work may have been merged elsewhere "
                "- inspect orchestrator_refs.json; nothing was verified",
            )
        if not report.changed_files_present:
            # A no-op run is its own fact and must not be described as unverified
            # CODE -- there is no code. The VerificationState vocabulary already
            # separates NOT_RUN_NO_CHANGE from NOT_RUN_SPEC_DEFECT, and
            # flattening that here would re-introduce a collapse one level down.
            return (
                VERIFY_STATE_NOT_ATTEMPTED,
                f"the CLI produced no changes, so there was nothing to "
                f"verify{detail} - `verify_passed=False` here means NOTHING WAS "
                "BUILT, not that a build failed",
            )
        return (
            VERIFY_STATE_NOT_ATTEMPTED,
            f"acceptance did not run{detail} - this run produced NO evidence "
            "about the code, and `verify_passed=False` here means COULD NOT "
            "CHECK, not FAILED",
        )
    if outcome == "failed":
        return (
            VERIFY_STATE_FAIL,
            "acceptance commands ran and did not pass - this IS a verdict "
            "about the code",
        )
    if report.protected_path_violations:
        return (
            VERIFY_STATE_FAIL,
            "acceptance passed but the change touches a protected path, which "
            "no acceptance result can override",
        )
    if not ref_guard_passed:
        return (
            VERIFY_STATE_FAIL,
            "acceptance passed but a ref outside the worktree moved or could "
            "not be read",
        )
    return (VERIFY_STATE_PASS, "acceptance commands ran and passed")


def _write_report(
    run_dir: Path,
    *,
    task: TaskSpec,
    cli_result: AdapterResult,
    report: VerifyReport,
    changed: list[str],
    worktree_path: Path,
    branch_publish: BranchPublishResult | None = None,
    post_run: Mapping[str, object] | None = None,
    orchestrator_ref_guard: Mapping[str, object] | None = None,
) -> None:
    file_lines: list[str] = [f"- `{path}`" for path in changed] or ["(none)"]
    classification = cli_result.classification
    lines: list[str] = [
        f"# Run report: {task.id} — {task.title}",
        "",
        f"- CLI: `{task.cli}`",
        f"- Resolved via: `{task.resolved_via or '(unspecified)'}`",
        f"- Branch: `{task.worktree_branch}`",
        f"- Target repo: `{task.target_repo}`",
        f"- Worktree: `{worktree_path}`",
        (
            f"- Model: `{task.model or '(default)'}` "
            f"reasoning=`{task.reasoning_effort or '(default)'}`"
        ),
        f"- CLI exit: `{cli_result.exit_code}` (timed_out={cli_result.timed_out})",
        f"- Duration: {cli_result.duration_seconds:.1f}s",
        f"- Post-run state: **{str((post_run or {}).get('state') or 'unknown').upper()}**",
        f"- Post-run started: `{(post_run or {}).get('started_at', 'not_recorded')}`",
        f"- Post-run ended: `{(post_run or {}).get('ended_at', 'not_recorded')}`",
        f"- Post-run duration: {(post_run or {}).get('duration_seconds', 0.0)}s",
        "- Supervising timeout: "
        f"{(post_run or {}).get('supervising_timeout_seconds', 'not_recorded')}s",
        "- Acceptance timeout: "
        f"{(post_run or {}).get('acceptance_timeout_seconds', 'not_recorded')}s",
        "",
        "## Classification",
        "",
    ]
    if task.allow_destructive_branch_reset:
        lines.extend(
            [
                (
                    "- Destructive branch reset opt-in: **ENABLED**. "
                    "Unreachable branch commits may be reset only after "
                    "rescue-ref creation."
                ),
                "",
            ]
        )
    if classification is not None:
        lines.append(f"- Kind: **{classification.kind.value}**")
        if classification.suggested_action:
            lines.append(f"- Suggested action: {classification.suggested_action}")
        if classification.matched_pattern:
            lines.append(f"- Matched pattern: `{classification.matched_pattern}`")
        if classification.quota_reset_window is not None:
            lines.append(
                f"- Quota reset window: `{classification.quota_reset_window}`"
            )
        if classification.quota_reset_window_provenance is not None:
            lines.append(
                "- Quota reset-window provenance: "
                f"**{classification.quota_reset_window_provenance.value}**"
            )
    else:
        lines.append("- (no classification recorded)")
    lines.extend(_codex_lifecycle_report_lines(cli_result))
    lines.extend(_context_timeout_report_lines(run_dir))
    lines.extend(
        [
            "",
            "## Changed files",
            "",
            *file_lines,
            *_surface_report_lines(
                changed=changed,
                out_of_scope_paths=report.out_of_scope_paths,
            ),
            "",
            "## Allowlist check",
            "",
            f"- Changed files present: **{report.changed_files_present}**",
            f"- Allowlist passed: **{report.allowlist_passed}**",
            f"- Forbidden violations: {report.forbidden_violations or 'none'}",
            f"- Out-of-scope paths: {report.out_of_scope_paths or 'none'}",
            "- Informational only: **does NOT gate verification**",
            "",
            "## Protected paths",
            "",
            (
                "- Protected-path violations: "
                f"{report.protected_path_violations or 'none'}"
            ),
            "",
            "## Acceptance commands",
            "",
            f"- Acceptance outcome: **{report.acceptance_outcome.upper()}**",
            "",
        ]
    )
    if not report.command_results:
        lines.extend(_acceptance_skip_report_lines(report, classification))
    else:
        for check in report.command_results:
            status = "PASS" if check.passed else "FAIL"
            lines.append(
                f"### `{check.name}` — {status} "
                f"[{check.classification}] ({check.duration_seconds:.1f}s)"
            )
            lines.append("")
            lines.append(
                "- Pytest counts: "
                + (
                    ", ".join(
                        f"{name}={count}"
                        for name, count in check.pytest_counts.items()
                    )
                    if isinstance(check.pytest_counts, dict)
                    else "UNAVAILABLE"
                )
            )
            lines.append(
                "- Minimum collected: "
                + (
                    str(check.acceptance_min_collected)
                    if check.acceptance_min_collected is not None
                    else "not declared"
                )
            )
            lines.append("")
            if check.details:
                lines.append("```")
                lines.append(check.details[-2000:])
                lines.append("```")
            lines.append("")

    lines.extend(["", "## Branch publication", ""])
    if branch_publish is None:
        lines.append("- Not requested.")
    else:
        lines.extend(
            [
                f"- Remote: `{branch_publish.remote_name}`",
                f"- Requested remote ref: `{branch_publish.requested_remote_ref}`",
                f"- Remote ref: `{branch_publish.remote_ref}`",
                f"- Pushed: **{branch_publish.pushed}**",
                f"- State: **{branch_publish.state}**",
            ]
        )
        if branch_publish.error:
            lines.append(f"- Error: `{branch_publish.error[:500]}`")

    lines.extend(_orchestrator_ref_report_lines(orchestrator_ref_guard))

    lines.append("## Overall")
    lines.append("")
    ref_guard_passed = bool(
        orchestrator_ref_guard is not None
        and orchestrator_ref_guard.get("passed") is True
    )
    state, reason = verification_verdict(report, ref_guard_passed=ref_guard_passed)
    passed_bool = report.passed and ref_guard_passed
    lines.append(f"- Verification: **{state}** - {reason}")
    if state == VERIFY_STATE_NOT_ATTEMPTED:
        lines.append(
            f"- Verify passed: **{passed_bool} (NOT A VERDICT - acceptance never ran)**"
        )
    else:
        lines.append(f"- Verify passed: **{passed_bool}**")
    _write_text_atomic(run_dir / "report.md", "\n".join(lines))


def _orchestrator_ref_report_lines(
    guard: Mapping[str, object] | None,
) -> list[str]:
    """Render before/after ref evidence even when the values are equal."""
    lines = ["", "## Orchestrator refs", ""]
    if guard is None:
        lines.extend(
            [
                "- Ref guard passed: **False**",
                "- Before/after state was not recorded.",
                "",
            ]
        )
        return lines

    before = guard.get("before")
    after = guard.get("after")
    before_refs = _reported_orchestrator_ref_values(before)
    after_refs = _reported_orchestrator_ref_values(after)
    passed_value = guard.get("passed", guard.get("clean"))
    passed = passed_value is True
    lines.append(f"- Ref guard passed: **{passed}**")
    for ref in (*ORCHESTRATOR_PROTECTED_REFS, *ORCHESTRATOR_OBSERVED_REMOTE_REFS):
        lines.append(f"- {ref} before: `{before_refs.get(ref) or 'unresolved'}`")
        lines.append(f"- {ref} after: `{after_refs.get(ref) or 'unresolved'}`")

    for label, snapshot in (("before", before), ("after", after)):
        if not isinstance(snapshot, Mapping):
            continue
        for ref, error in dict(snapshot.get("errors") or {}).items():
            lines.append(f"- {ref} {label} error: `{error}`")

    findings = [str(item) for item in list(guard.get("findings") or [])]
    if findings:
        lines.append("- **A REF OUTSIDE THE WORKTREE MOVED OR COULD NOT BE READ:**")
        lines.extend(f"  - {finding}" for finding in findings)
        lines.append(
            "  - A worktree isolates files, not git refs. Review before "
            "trusting this run."
        )
    else:
        lines.append("- Findings: none")
    lines.append("")
    return lines


def _reported_orchestrator_ref_values(snapshot: object) -> dict[str, object]:
    """Read the ``refs`` map out of one ref-guard snapshot."""
    if not isinstance(snapshot, Mapping):
        return {}
    refs = snapshot.get("refs")
    if isinstance(refs, Mapping):
        return dict(refs)
    return {}


def _surface_report_lines(
    *,
    changed: list[str],
    out_of_scope_paths: list[str],
) -> list[str]:
    """Describe actual files outside the task-authored surface prediction."""

    actual_count = len(changed)
    predicted_count = actual_count - len(out_of_scope_paths)
    delta_counts: dict[str, int] = {}
    for path in out_of_scope_paths:
        directory = _surface_delta_directory(path)
        delta_counts[directory] = delta_counts.get(directory, 0) + 1

    lines = [
        "",
        "## Surface",
        "",
        (
            f"SURFACE: {actual_count} files changed, "
            f"spec predicted {predicted_count}"
        ),
    ]
    for directory, count in sorted(
        delta_counts.items(), key=lambda item: (-item[1], item[0])
    ):
        unit = "file" if count == 1 else "files"
        lines.append(f"  + {directory} ({count} {unit})")
    if not delta_counts:
        lines.append("  (no unpredicted directories)")
    lines.append("  -> informational; does NOT gate")
    return lines


def _surface_delta_directory(path: str) -> str:
    """Collapse an unpredicted path to its repository-level owner."""

    normalized = path.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) <= 1:
        return normalized or "(unknown)"
    depth = 2 if parts[0] in {"libs", "services"} and len(parts) > 2 else 1
    return "/".join(parts[:depth]) + "/**"


def _acceptance_skip_report_lines(
    report: VerifyReport,
    classification: Classification | None,
) -> list[str]:
    """Return the human-readable skip-cause line(s) for the report.

    The reasons are already recorded on ``report.acceptance_skip_reason``;
    this helper only formats them so the reader can tell which gate fired.
    """
    if report.acceptance_skip_reason is None:
        return []

    parts: list[str] = []
    for reason in report.acceptance_skip_reason:
        if reason == "cli_failed":
            kind = classification.kind.value if classification else "unknown"
            parts.append(f"CLI did not succeed cleanly: classification={kind}")
        elif reason == "no_files_changed":
            parts.append("no files changed")
        elif reason == "allowlist_failed":
            detail_parts: list[str] = []
            if report.out_of_scope_paths:
                detail_parts.append(
                    f"{len(report.out_of_scope_paths)} changed file(s) "
                    "outside allowed_paths"
                )
            if report.forbidden_violations:
                detail_parts.append(
                    f"{len(report.forbidden_violations)} forbidden path(s)"
                )
            if not detail_parts:
                detail_parts.append("check did not pass")
            parts.append("allowlist check failed: " + ", ".join(detail_parts))
        elif reason == "unknown":
            parts.append("reason unknown")
        else:
            parts.append(reason)
    return ["(skipped — " + "; ".join(parts) + ")"]


def _codex_lifecycle_report_lines(cli_result: AdapterResult) -> list[str]:
    if cli_result.final_turn_status is None:
        return []

    lines = [
        "",
        "## Codex Turn Lifecycle",
        "",
        f"- Final turn status: `{cli_result.final_turn_status}`",
    ]
    if cli_result.token_usage is not None:
        usage = cli_result.token_usage
        lines.append(
            "- Token usage: "
            f"input={usage.get('input', 0)} "
            f"output={usage.get('output', 0)} "
            f"total={usage.get('total', 0)}"
        )
    if cli_result.error_info is not None:
        error = cli_result.error_info
        message = str(error.get("message") or "").strip()
        lines.append(
            "- Error info: "
            f"message=`{message[:300] or '(none)'}` "
            f"httpStatusCode=`{error.get('httpStatusCode')}` "
            f"errorCode=`{error.get('errorCode')}`"
        )
    if cli_result.idle_classification is not None:
        lines.append(f"- Idle classification: `{cli_result.idle_classification}`")
    if cli_result.turn_lifecycle:
        lines.append(f"- Lifecycle events: {len(cli_result.turn_lifecycle)}")
        tail_events = cli_result.turn_lifecycle[-5:]
        event_names = ", ".join(str(event.get("event")) for event in tail_events)
        lines.append(f"- Recent events: {event_names}")
    return lines


def _print_doctor() -> None:
    for row in cli_doctor():
        print(f"- {row['cli']}")
        for key, value in row.items():
            if key == "cli":
                continue
            print(f"    {key}: {value}")


def _print_models() -> None:
    from atlas_dispatch.adapter import MODELS

    for name in list_supported_models():
        definition = MODELS[name]
        print(f"- {name}")
        print(f"    cli: {definition.cli}")
        print(f"    model_id: {definition.model_id}")
        print(f"    reasoning_effort: {definition.reasoning_effort or '(default)'}")
        if definition.description:
            print(f"    description: {definition.description}")


def _print_clis() -> None:
    for name in list_supported_clis():
        definition = CLIS[name]
        print(f"- {name} (executable: {definition.executable})")


def _print_capabilities(cli: str) -> int:
    from atlas_dispatch.adapter import MODELS

    cli = cli.lower()
    if cli not in CLIS:
        print(f"unknown cli: {cli}", file=sys.stderr)
        return 1

    definition = CLIS[cli]
    models = sorted(m.name for m in MODELS.values() if m.cli == cli)
    models_str = ", ".join(models) if models else "(none)"

    print(f"- cli: {definition.name}")
    print(f"    executable: {definition.executable}")
    print(f"    argv_template: {' '.join(definition.argv_template)}")
    print(f"    auth_setup_hint: {definition.auth_setup_hint}")
    print(
        f"    extra_pattern_counts: "
        f"auth={len(definition.extra_auth_patterns)} "
        f"rate_limit={len(definition.extra_rate_limit_patterns)} "
        f"overloaded={len(definition.extra_overloaded_patterns)} "
        f"refusal={len(definition.extra_refusal_patterns)}"
    )
    print(f"    reads_prompt_from_stdin: {definition.reads_prompt_from_stdin}")
    print(f"    models: {models_str}")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="atlas-dispatch",
        description=(
            "Dispatch a task spec to a coding-agent CLI in an isolated worktree. "
            "Any orchestrator (an AI agent, a script, or a person at a shell) "
            "can invoke this dispatcher; the dispatched CLI runs in a fresh "
            "session with full-access flags inside a per-task git worktree, and "
            "the harness never merges."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="Run a single task spec")
    run_parser.add_argument(
        "spec",
        type=Path,
        help="Path to a task spec JSON file.",
    )
    run_parser.add_argument(
        "--no-reuse",
        action="store_true",
        help="Require a fresh CLI invocation even when an identical run exists.",
    )

    sub.add_parser("doctor", help="Show CLI availability, git, and protected paths")
    sub.add_parser("clis", help="List supported CLIs (one per line)")
    sub.add_parser("models", help="List registered model identifiers with their CLIs")
    capabilities_parser = sub.add_parser(
        "capabilities",
        help="Show what atlas-dispatch knows about a CLI (argv template, patterns, models)",
    )
    capabilities_parser.add_argument(
        "cli",
        type=str,
        help="CLI name (e.g. codex, claude, gemini, kimi-code, grok, hermes)",
    )

    args = parser.parse_args(raw_argv)

    if args.cmd == "run":
        return dispatch(args.spec, no_reuse=args.no_reuse)

    if args.cmd == "doctor":
        from atlas_dispatch.doctor import run_doctor

        return run_doctor()

    if args.cmd == "models":
        _print_models()
        return 0

    if args.cmd == "clis":
        _print_clis()
        return 0

    if args.cmd == "capabilities":
        return _print_capabilities(args.cli)

    parser.error("unknown command")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
