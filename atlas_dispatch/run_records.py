"""``cli.summary.json`` and the other records a dispatched run writes.

The CLI result is persisted as a provisional ``post_run_incomplete`` record
before verification starts, and replaced by the verdict only when
verification completes, so a crash or kill mid-verification can never be
mistaken for a verdict. ``post_run.json`` carries a heartbeat for the same
reason.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from atlas_dispatch.adapter import AdapterResult
from atlas_dispatch.artifacts import (
    POST_RUN_HEARTBEAT_ARTIFACT,
    POST_RUN_HEARTBEAT_INTERVAL_SECONDS,
    POST_RUN_INCOMPLETE_CLASSIFICATION,
    POST_RUN_INCOMPLETE_MARKER,
    POST_RUN_INCOMPLETE_VERIFICATION_STATE,
    ChangedFilesEvidence,
    _read_json_object,
    _set_changed_files_summary,
    _write_json_atomic,
    _write_text_artifact,
    _write_text_atomic,
)
from atlas_dispatch.git_exec import git_result_error, run_git_in
from atlas_dispatch.reporting import _write_post_run_report
from atlas_dispatch.task_spec import REUSE_POLICY_NEVER, TaskSpec
from atlas_dispatch.verify import CheckResult, VerifyReport
from atlas_dispatch.worktree import BranchPublishResult, Worktree

LOGGER = logging.getLogger(__name__)


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


def _persist_cli_result(
    run_dir: Path,
    result: AdapterResult,
    *,
    task: TaskSpec | None = None,
    review_model_evidence: Mapping[str, object] | None = None,
    orchestrator_ref_guard: Mapping[str, object] | None = None,
) -> None:
    """Persist the CLI observation as a provisional, not-yet-verified record.

    Without ``task`` (a caller outside the dispatch pipeline) only the CLI
    observation is written, and the record does not claim a post-run phase.
    """

    (run_dir / "cli.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "cli.stderr.txt").write_text(result.stderr, encoding="utf-8")
    classification_payload = _classification_payload(result)
    summary = _cli_observation_summary(
        result,
        classification_payload=classification_payload,
        review_model_evidence=review_model_evidence,
        orchestrator_ref_guard=orchestrator_ref_guard,
    )
    if result.run_identity is not None:
        (run_dir / "run-identity.json").write_text(
            json.dumps(result.run_identity, indent=2), encoding="utf-8"
        )
    if task is None:
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
        _provisional_post_run_fields(
            run_dir=run_dir,
            task=task,
            classification_payload=classification_payload,
        )
    )
    # This first-phase record is not a verification verdict. Keep the marker
    # until _persist_verification_summary durably writes the post-run fields.
    summary[POST_RUN_INCOMPLETE_MARKER] = True
    _write_json_atomic(run_dir / "cli.summary.json", summary)
    _write_post_run_report(run_dir=run_dir, task=task, summary=summary)


def _classification_payload(result: AdapterResult) -> dict[str, Any] | None:
    classification = result.classification
    if classification is None:
        return None
    payload: dict[str, Any] = {
        "kind": classification.kind.value,
        "suggested_action": classification.suggested_action,
        "matched_pattern": classification.matched_pattern,
    }
    if classification.quota_reset_window is not None:
        payload["quota_reset_window"] = classification.quota_reset_window
    if classification.quota_reset_window_provenance is not None:
        payload["quota_reset_window_provenance"] = (
            classification.quota_reset_window_provenance.value
        )
    return payload


def _cli_observation_summary(
    result: AdapterResult,
    *,
    classification_payload: dict[str, Any] | None,
    review_model_evidence: Mapping[str, object] | None,
    orchestrator_ref_guard: Mapping[str, object] | None,
) -> dict[str, Any]:
    """What the CLI process did: exit, timing, argv, classification, lifecycle."""

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
    return summary


def _provisional_post_run_fields(
    *,
    run_dir: Path,
    task: TaskSpec,
    classification_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Summary fields that say "no verdict yet" until verification completes."""

    return {
        "classification": {
            "kind": POST_RUN_INCOMPLETE_CLASSIFICATION,
            "suggested_action": (
                "Preserve the published branch and inspect post_run state; "
                "never treat this as an acceptance failure or redispatch it."
            ),
            "matched_pattern": None,
        },
        "cli_classification": classification_payload,
        # Deliberately non-null and non-verifying. A consumer that only knows
        # the completed-run fields cannot read an unrecognised
        # verification_state as a pass, and acceptance_failed=False keeps the
        # absent verdict from reading as VERIFIED_FAIL.
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


def _persist_changed_files_summary(
    run_dir: Path,
    evidence: ChangedFilesEvidence,
) -> None:
    summary_path = run_dir / "cli.summary.json"
    summary = _read_json_object(summary_path)
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
    summary = _read_json_object(summary_path)
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
    summary = _read_json_object(summary_path)
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
        # Do NOT add a `verification_state` key here. In cli.summary.json that
        # key only ever marks a record that is NOT a verdict (POST_RUN_INCOMPLETE,
        # or UNKNOWN after a dispatch exception), and completion removes it. The
        # machine-readable truth about "could not check" is carried by
        # acceptance_outcome, acceptance_skip_reason and
        # acceptance_not_attempted_reason below; the human headline is rendered
        # in reporting._write_report.
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
        head_result = run_git_in(worktree.worktree_path, ["rev-parse", "HEAD"])
        short_result = run_git_in(
            worktree.worktree_path, ["rev-parse", "--short", "HEAD"]
        )

        head_ref = "HEAD"
        errors: list[str] = []
        if head_result.returncode == 0 and head_result.stdout.strip():
            head_ref = head_result.stdout.strip()
        else:
            errors.append(f"git rev-parse HEAD failed: {git_result_error(head_result)}")
        if short_result.returncode == 0 and short_result.stdout.strip():
            short_sha = short_result.stdout.strip()
        else:
            errors.append(
                f"git rev-parse --short HEAD failed: {git_result_error(short_result)}"
            )

        diff_args = ["diff", f"{base_ref}...{head_ref}"]
        diff_result = run_git_in(worktree.worktree_path, diff_args)
        if diff_result.returncode == 0:
            content = diff_result.stdout
        else:
            errors.append(
                f"git {' '.join(diff_args)} failed: {git_result_error(diff_result)}"
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
