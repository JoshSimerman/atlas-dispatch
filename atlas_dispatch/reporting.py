"""``report.md``: the human-readable account of a run.

``cli.summary.json`` carries the same facts for machines and is authoritative.
The report never states ``verify_passed`` without saying which of the three
verdicts it means (ADR-004).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from atlas_dispatch.adapter import AdapterResult, Classification
from atlas_dispatch.artifacts import (
    POST_RUN_INCOMPLETE_CLASSIFICATION,
    ChangedFilesEvidence,
    _changed_files_report_lines,
    _read_changed_files_evidence,
    _write_text_atomic,
)
from atlas_dispatch.ref_guard import _orchestrator_ref_report_lines
from atlas_dispatch.task_spec import TaskSpec
from atlas_dispatch.verify import VerifyReport
from atlas_dispatch.worktree import BranchPublishResult, worktree_path_for

# A BOOLEAN CANNOT CARRY THREE FACTS. `verify_passed=False` conflates "the code
# failed verification" with "verification never happened", and whoever reads it
# decides whether to redispatch. The default reading of False is "it failed,
# start over", which would discard a complete, coherent change whose acceptance
# suite merely never ran (for example, the run hit its timeout mid-suite).
#
# The boolean stays in cli.summary.json for machines that gate on it; the report
# never states it without saying which of these three verdicts it means.
VERIFY_STATE_PASS = "VERIFIED_PASS"
VERIFY_STATE_FAIL = "VERIFIED_FAIL"
VERIFY_STATE_NOT_ATTEMPTED = "NOT_ATTEMPTED"


def verification_verdict(
    report: VerifyReport, *, ref_guard_passed: bool
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
            # CODE -- there is no code. verify.VerificationState keeps the same
            # distinction (NOT_RUN_NO_CHANGE).
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
    """Write the final report for a run that completed verification."""

    classification = cli_result.classification
    lines: list[str] = [
        *_task_header_lines(task, worktree_path=worktree_path),
        f"- CLI exit: `{cli_result.exit_code}` (timed_out={cli_result.timed_out})",
        f"- Duration: {cli_result.duration_seconds:.1f}s",
        *_post_run_timing_lines(post_run or {}),
        "",
        "## Classification",
        "",
        *_classification_lines(task, classification),
        *_codex_lifecycle_report_lines(cli_result),
        *_context_timeout_report_lines(run_dir),
        *_changed_files_and_path_check_lines(changed, report),
        *_acceptance_result_lines(report, classification),
        *_branch_publication_lines(branch_publish),
        *_orchestrator_ref_report_lines(orchestrator_ref_guard),
        *_overall_lines(report, orchestrator_ref_guard),
    ]
    _write_text_atomic(run_dir / "report.md", "\n".join(lines))


def _task_header_lines(task: TaskSpec, *, worktree_path: Path | None) -> list[str]:
    """Title and the task identity lines every full report starts with."""

    lines = [
        f"# Run report: {task.id} — {task.title}",
        "",
        f"- CLI: `{task.cli}`",
        f"- Resolved via: `{task.resolved_via or '(unspecified)'}`",
        f"- Branch: `{task.worktree_branch}`",
        f"- Target repo: `{task.target_repo}`",
    ]
    if worktree_path is not None:
        lines.append(f"- Worktree: `{worktree_path}`")
    lines.append(
        f"- Model: `{task.model or '(default)'}` "
        f"reasoning=`{task.reasoning_effort or '(default)'}`"
    )
    return lines


def _post_run_timing_lines(post_run: Mapping[str, object]) -> list[str]:
    return [
        f"- Post-run state: **{str(post_run.get('state') or 'unknown').upper()}**",
        f"- Post-run started: `{post_run.get('started_at', 'not_recorded')}`",
        f"- Post-run ended: `{post_run.get('ended_at', 'not_recorded')}`",
        f"- Post-run duration: {post_run.get('duration_seconds', 0.0)}s",
        "- Supervising timeout: "
        f"{post_run.get('supervising_timeout_seconds', 'not_recorded')}s",
        "- Acceptance timeout: "
        f"{post_run.get('acceptance_timeout_seconds', 'not_recorded')}s",
    ]


def _classification_lines(
    task: TaskSpec, classification: Classification | None
) -> list[str]:
    lines: list[str] = []
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
    if classification is None:
        lines.append("- (no classification recorded)")
        return lines
    lines.append(f"- Kind: **{classification.kind.value}**")
    if classification.suggested_action:
        lines.append(f"- Suggested action: {classification.suggested_action}")
    if classification.matched_pattern:
        lines.append(f"- Matched pattern: `{classification.matched_pattern}`")
    if classification.quota_reset_window is not None:
        lines.append(f"- Quota reset window: `{classification.quota_reset_window}`")
    if classification.quota_reset_window_provenance is not None:
        lines.append(
            "- Quota reset-window provenance: "
            f"**{classification.quota_reset_window_provenance.value}**"
        )
    return lines


def _changed_files_and_path_check_lines(
    changed: list[str], report: VerifyReport
) -> list[str]:
    return [
        "",
        "## Changed files",
        "",
        *([f"- `{path}`" for path in changed] or ["(none)"]),
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
    ]


def _acceptance_result_lines(
    report: VerifyReport, classification: Classification | None
) -> list[str]:
    lines = [
        "",
        "## Acceptance commands",
        "",
        f"- Acceptance outcome: **{report.acceptance_outcome.upper()}**",
        "",
    ]
    if not report.command_results:
        return lines + _acceptance_skip_report_lines(report, classification)
    for check in report.command_results:
        status = "PASS" if check.passed else "FAIL"
        pytest_counts = (
            ", ".join(f"{name}={count}" for name, count in check.pytest_counts.items())
            if isinstance(check.pytest_counts, dict)
            else "UNAVAILABLE"
        )
        minimum = (
            str(check.acceptance_min_collected)
            if check.acceptance_min_collected is not None
            else "not declared"
        )
        lines.extend(
            [
                f"### `{check.name}` — {status} "
                f"[{check.classification}] ({check.duration_seconds:.1f}s)",
                "",
                f"- Pytest counts: {pytest_counts}",
                f"- Minimum collected: {minimum}",
                "",
            ]
        )
        if check.details:
            lines.extend(["```", check.details[-2000:], "```"])
        lines.append("")
    return lines


def _branch_publication_lines(branch_publish: BranchPublishResult | None) -> list[str]:
    lines = ["", "## Branch publication", ""]
    if branch_publish is None:
        lines.append("- Not requested.")
        return lines
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
    return lines


def _overall_lines(
    report: VerifyReport, orchestrator_ref_guard: Mapping[str, object] | None
) -> list[str]:
    ref_guard_passed = bool(
        orchestrator_ref_guard is not None
        and orchestrator_ref_guard.get("passed") is True
    )
    state, reason = verification_verdict(report, ref_guard_passed=ref_guard_passed)
    passed_bool = report.passed and ref_guard_passed
    lines = ["## Overall", "", f"- Verification: **{state}** - {reason}"]
    if state == VERIFY_STATE_NOT_ATTEMPTED:
        lines.append(
            f"- Verify passed: **{passed_bool} (NOT A VERDICT - acceptance never ran)**"
        )
    else:
        lines.append(f"- Verify passed: **{passed_bool}**")
    return lines


def _write_cap_refusal_report(
    *,
    run_dir: Path,
    task: TaskSpec,
    cap: int,
    window_seconds: int,
    prior_run_paths: list[str],
    suggested_action: str,
) -> None:
    lines = [
        *_task_header_lines(task, worktree_path=None),
        "- CLI invocation: **REFUSED**",
        "- CLI exit: `1` (timed_out=False)",
        "- Duration: 0.0s",
        "",
        "## Consecutive-dispatch cap",
        "",
        f"- Task: `{task.id}`",
        f"- Base ref: `{task.base_ref}`",
        f"- Consecutive recent runs: **{len(prior_run_paths)}**",
        f"- Cap: **{cap}**",
        f"- Window: **{window_seconds} seconds**",
        "- No CLI was invoked for this attempt.",
        "- Retry policy: **stop retrying and investigate**.",
        f"- Suggested action: {suggested_action}",
        "",
        "### Prior run paths",
        "",
        *[f"- `{path}`" for path in prior_run_paths],
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
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


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
    lines = [
        *_task_header_lines(
            task,
            worktree_path=worktree_path_for(task.target_repo, task.worktree_branch),
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
        *_changed_files_report_lines(changed_evidence),
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
