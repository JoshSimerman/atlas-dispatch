"""Complete run records for attempts that end outside the normal pipeline.

A refused attempt (runaway cap), a reused verified run, and a dispatch that
raised all still get a full run directory with a summary and a report, so
every attempt ends in exactly one terminal record.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from atlas_dispatch.adapter import DispatchErrorKind
from atlas_dispatch.artifacts import (
    POST_RUN_INCOMPLETE_MARKER,
    _read_changed_files_evidence,
    _read_json_object,
    _read_text_artifact,
    _set_changed_files_summary,
    _unknown_changed_files_evidence,
    _write_changed_files_evidence,
    _write_json_atomic,
)
from atlas_dispatch.base_sync import PreDispatchGitSyncError
from atlas_dispatch.git_exec import git_result_error, run_git_in
from atlas_dispatch.reporting import (
    _write_cap_refusal_report,
    _write_dispatch_exception_report,
    _write_duplicate_satisfied_report,
    _write_post_run_report,
)
from atlas_dispatch.run_history import (
    ALLOW_CONSECUTIVE_DISPATCH_ENV,
    CONSECUTIVE_DISPATCH_CAP_REFUSAL_MARKER,
    _new_run_dir,
)
from atlas_dispatch.task_spec import TaskSpec

LOGGER = logging.getLogger(__name__)


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
        "consecutive_recent_runs": len(prior_runs),
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
    _write_cap_refusal_report(
        run_dir=run_dir,
        task=task,
        cap=cap,
        window_seconds=window_seconds,
        prior_run_paths=prior_run_paths,
        suggested_action=suggested_action,
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

    summary = _duplicate_run_summary(
        task=task,
        prior_run=prior_run,
        prior_summary=prior_summary,
        source_base_sha=source_base_sha,
    )
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


def _duplicate_run_summary(
    *,
    task: TaskSpec,
    prior_run: Path,
    prior_summary: dict[str, Any],
    source_base_sha: str,
) -> dict[str, Any]:
    """A success summary that carries the prior run's verification verbatim."""

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
    return summary


def _copy_prior_run_artifact(source: Path, run_dir: Path) -> None:
    try:
        content = source.read_bytes()
    except OSError:
        return
    try:
        (run_dir / source.name).write_bytes(content)
    except OSError as exc:
        LOGGER.warning("could not copy prior run artifact %s: %s", source, exc)


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
    _complete_exception_run_shell(run_dir=run_dir, task=task, prompt=prompt)
    error_text = f"{type(error).__name__}: {error}"

    changed_evidence = _read_changed_files_evidence(run_dir / "changed_files.txt")
    if changed_evidence.state == "unknown":
        stage = "diff_not_observed_after_cli_invocation" if cli_invoked else "cli_not_invoked"
        changed_evidence = _unknown_changed_files_evidence(
            f"{stage}: dispatch_exception: {error_text}"
        )
        _write_changed_files_evidence(run_dir, changed_evidence)

    summary_path = run_dir / "cli.summary.json"
    summary = _read_json_object(summary_path)
    post_run = summary.get("post_run")
    if (
        summary.get(POST_RUN_INCOMPLETE_MARKER) is True
        and isinstance(post_run, dict)
        and post_run.get("state") in {"running", "incomplete"}
    ):
        # The post-run handler already persisted the more precise failure. Do
        # not flatten it into dispatch_exception or acceptance_failed: an
        # absent acceptance verdict is not a failing acceptance verdict.
        summary["dispatch_error"] = error_text
        _set_changed_files_summary(summary, changed_evidence)
        summary["branch_reconciliation"] = _reconcile_published_branch(task)
        _write_json_atomic(summary_path, summary)
        _write_post_run_report(run_dir=run_dir, task=task, summary=summary)
        print(
            "[atlas-dispatch] post_run_incomplete "
            f"task={task.id} cli_invoked={cli_invoked} run_dir={run_dir}: "
            f"{error_text}"
        )
        return run_dir

    _apply_dispatch_exception_fields(
        summary, task=task, error=error, cli_invoked=cli_invoked
    )
    _set_changed_files_summary(summary, changed_evidence)
    branch_reconciliation = _reconcile_published_branch(task)
    summary["branch_reconciliation"] = branch_reconciliation
    if branch_reconciliation.get("published") is True:
        (run_dir / "branch_publish.json").write_text(
            json.dumps(_preexisting_branch_publish(task, branch_reconciliation), indent=2),
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
        f"{error_text}"
    )
    return run_dir


def _complete_exception_run_shell(*, run_dir: Path, task: TaskSpec, prompt: str) -> None:
    """Fill in whichever standard artifacts the aborted attempt did not write."""

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


def _apply_dispatch_exception_fields(
    summary: dict[str, Any],
    *,
    task: TaskSpec,
    error: Exception,
    cli_invoked: bool,
) -> None:
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


def _preexisting_branch_publish(
    task: TaskSpec, branch_reconciliation: dict[str, Any]
) -> dict[str, Any]:
    """A branch_publish record for a remote branch this attempt did not push."""

    return {
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
        "nonzero_diff_vs_base": branch_reconciliation.get("nonzero_diff_vs_base"),
    }


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
        remote_result = run_git_in(
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
            "reason": f"remote branch lookup failed: {git_result_error(remote_result)}",
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
    diff_result = run_git_in(
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
