"""The dispatch pipeline: one task spec in, one run record out. It never merges.

``dispatch()`` loads a spec, applies the runaway caps and duplicate-run reuse,
then runs a new attempt:

1. sync the base branch and create the task's worktree;
2. run the CLI, snapshotting the orchestrator's refs before and after;
3. persist a provisional record, auto-commit, record the changed files and,
   if requested, publish the branch;
4. verify the committed result (path checks, acceptance) under a heartbeat;
5. persist the verdict and write the report.

The steps live in the modules imported below; this module only sequences
them. docs/ARCHITECTURE.md lists every step in code order.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from atlas_dispatch import import_provenance
from atlas_dispatch.adapter import (
    AdapterResult,
    Classification,
    DispatchErrorKind,
    ResolvedInvocation,
    inject_mcp_config,
    list_supported_clis,
    resolve_invocation,
    run_cli,
)
from atlas_dispatch.artifacts import (
    _known_changed_files_evidence,
    _write_changed_files_evidence,
)
from atlas_dispatch.base_sync import (
    PreDispatchGitSyncError,
    _pre_dispatch_sync_base_ref,
)
from atlas_dispatch.cli import main
from atlas_dispatch.mcp_config import (
    McpRuntime,
    _extend_git_info_exclude,
    _prepare_mcp_runtime,
    _render_mcp_servers,
)
from atlas_dispatch.prompt import (
    CONTEXT_FILE_READ_TIMEOUT_SECONDS,
    _build_context_block,
    _find_unfilled_prompt_placeholders,
    render_prompt,
)
from atlas_dispatch.ref_guard import (
    ORCHESTRATOR_OBSERVED_REMOTE_REFS,
    ORCHESTRATOR_PROTECTED_REFS,
    _capture_orchestrator_refs,
    _diff_orchestrator_refs,
    _orchestrator_ref_report_lines,
    _record_orchestrator_ref_guard,
)
from atlas_dispatch.reporting import (
    VERIFY_STATE_FAIL,
    VERIFY_STATE_NOT_ATTEMPTED,
    VERIFY_STATE_PASS,
    _write_report,
    verification_verdict,
)
from atlas_dispatch.run_history import (
    ABSOLUTE_DISPATCH_RUN_CAP,
    ALLOW_CONSECUTIVE_DISPATCH_ENV,
    ALLOW_DUPLICATE_RUN_ENV,
    CONSECUTIVE_DISPATCH_CAP,
    CONSECUTIVE_DISPATCH_WINDOW,
    _dispatch_runs_lock,
    _new_run_dir,
    _parse_run_dir_timestamp,
    _prior_successful_run,
    _recent_consecutive_dispatch_runs,
    _resolve_source_base_sha,
    _total_run_dir_count,
)
from atlas_dispatch.run_outcomes import (
    _escalate_consecutive_dispatch_cap,
    _raise_dispatch_error_with_artifact,
    _write_dispatch_exception_run,
    _write_duplicate_satisfied_run,
)
from atlas_dispatch.run_records import (
    _acceptance_command_summary,
    _persist_branch_publish,
    _persist_changed_files_summary,
    _persist_cli_result,
    _persist_post_run_incomplete,
    _persist_post_run_started,
    _persist_verification_summary,
    _PostRunHeartbeat,
    _verification_summary,
    _write_combined_diff_artifact,
)
from atlas_dispatch.task_spec import (
    CODE_ROOTS_ENV,
    REUSE_POLICY_NEVER,
    TaskSpec,
    _code_roots,
    _default_runs_dir,
    _expected_review_deliverable_path,
    load_task,
)
from atlas_dispatch.verify import (
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
)
from atlas_dispatch.worktree_state import (
    NO_CHANGE_SUGGESTED_ACTION,
    _commits_ahead_of_base,
    _has_uncommitted_work,
    _resolve_verified_head_sha,
    _should_classify_no_change,
    _worktree_head_sha,
)

LOGGER = logging.getLogger(__name__)

# The pipeline's entry points, plus names re-exported from the modules the
# pipeline is built from, so ``atlas_dispatch.dispatcher`` stays a single
# import point for callers and tests.
__all__ = [
    "CODE_ROOTS_ENV",
    "CONTEXT_FILE_READ_TIMEOUT_SECONDS",
    "ORCHESTRATOR_OBSERVED_REMOTE_REFS",
    "ORCHESTRATOR_PROTECTED_REFS",
    "VERIFY_STATE_FAIL",
    "VERIFY_STATE_NOT_ATTEMPTED",
    "VERIFY_STATE_PASS",
    "McpRuntime",
    "PreDispatchGitSyncError",
    "TaskSpec",
    "_acceptance_command_summary",
    "_build_context_block",
    "_capture_orchestrator_refs",
    "_code_roots",
    "_commits_ahead_of_base",
    "_diff_orchestrator_refs",
    "_expected_review_deliverable_path",
    "_extend_git_info_exclude",
    "_find_unfilled_prompt_placeholders",
    "_has_uncommitted_work",
    "_orchestrator_ref_report_lines",
    "_parse_run_dir_timestamp",
    "_persist_cli_result",
    "_pre_dispatch_sync_base_ref",
    "_prepare_mcp_runtime",
    "_publish_branch_if_requested",
    "_render_mcp_servers",
    "_should_classify_no_change",
    "_verification_summary",
    "_worktree_head_sha",
    "check_protected_paths",
    "dispatch",
    "load_task",
    "main",
    "render_prompt",
    "run_acceptance_commands",
    "verification_verdict",
]


@dataclass(kw_only=True)
class _DispatchProgress:
    cli_invoked: bool = False


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


@dataclass(frozen=True, kw_only=True)
class _CapCheck:
    """Outcome of the runaway caps: a refusal exit code, or permission to go on."""

    refused_exit_code: int | None = None
    override_applied: bool = False


def dispatch(
    spec_path: Path,
    *,
    heartbeat: Any | None = None,
    no_reuse: bool = False,
) -> int:
    """Dispatch one task spec. Returns 0 only when the CLI succeeded and verification passed."""

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
    with _dispatch_runs_lock(runs_root):
        cap_check = _check_runaway_caps(task=task, runs_root=runs_root, prompt=prompt)
        if cap_check.refused_exit_code is not None:
            return cap_check.refused_exit_code
        duplicate_run_override_applied = (
            os.environ.get(ALLOW_DUPLICATE_RUN_ENV, "").strip() == "1"
        )
        if duplicate_run_override_applied and not cap_check.override_applied:
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
        prepared_base_ref: str | None = None
        if (
            task.reuse_policy != REUSE_POLICY_NEVER
            and not duplicate_run_override_applied
            and not cap_check.override_applied
        ):
            prepared_base_ref, reused_exit_code = _reuse_prior_verified_run(
                task=task,
                runs_root=runs_root,
                prompt=prompt,
            )
            if reused_exit_code is not None:
                return reused_exit_code
        return _dispatch_new_run(
            task=task,
            runs_root=runs_root,
            prepared_base_ref=prepared_base_ref,
            prompt=prompt,
            heartbeat=heartbeat,
        )


def _check_runaway_caps(*, task: TaskSpec, runs_root: Path, prompt: str) -> _CapCheck:
    """Apply both runaway caps (ADR-010); a refusal is itself a complete run record."""

    override_requested = (
        os.environ.get(ALLOW_CONSECUTIVE_DISPATCH_ENV, "").strip() == "1"
    )
    override_applied = False

    def refuse(prior_runs: list[Path], cap: int) -> _CapCheck:
        return _CapCheck(
            refused_exit_code=_escalate_consecutive_dispatch_cap(
                task=task,
                runs_root=runs_root,
                prior_runs=prior_runs,
                cap=cap,
                window=CONSECUTIVE_DISPATCH_WINDOW,
                prompt=prompt,
            )
        )

    # BACKSTOP FIRST, and deliberately before anything that reads a clock:
    # if every time-based control is defeated, this one still counts.
    absolute_runs = _total_run_dir_count(runs_root, cap=ABSOLUTE_DISPATCH_RUN_CAP)
    if absolute_runs >= ABSOLUTE_DISPATCH_RUN_CAP:
        if not override_requested:
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
            return refuse([], ABSOLUTE_DISPATCH_RUN_CAP)
        override_applied = True
        message = (
            "[atlas-dispatch] absolute run cap override honoured: "
            f"{ALLOW_CONSECUTIVE_DISPATCH_ENV}=1; task={task.id} "
            f"run_dirs={absolute_runs} cap={ABSOLUTE_DISPATCH_RUN_CAP}"
        )
        print(message)
        LOGGER.warning(message)

    recent_runs = _recent_consecutive_dispatch_runs(
        runs_root=runs_root,
        task=task,
        window=CONSECUTIVE_DISPATCH_WINDOW,
    )
    if recent_runs is None:
        if override_applied:
            return _CapCheck(override_applied=True)
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
        return refuse([], CONSECUTIVE_DISPATCH_CAP)
    if len(recent_runs) >= CONSECUTIVE_DISPATCH_CAP:
        if not override_requested:
            return refuse(recent_runs, CONSECUTIVE_DISPATCH_CAP)
        override_applied = True
        prior_paths = ", ".join(str(path.resolve()) for path in recent_runs)
        message = (
            "[atlas-dispatch] consecutive-dispatch override honoured: "
            f"{ALLOW_CONSECUTIVE_DISPATCH_ENV}=1; CLI invocation allowed "
            f"for task={task.id} base_ref={task.base_ref} "
            f"consecutive_recent_runs={len(recent_runs)} "
            f"cap={CONSECUTIVE_DISPATCH_CAP} "
            f"window_seconds={int(CONSECUTIVE_DISPATCH_WINDOW.total_seconds())} "
            f"prior_run_paths=[{prior_paths}]"
        )
        print(message)
        LOGGER.warning(message)
    return _CapCheck(override_applied=override_applied)


def _reuse_prior_verified_run(
    *, task: TaskSpec, runs_root: Path, prompt: str
) -> tuple[str | None, int | None]:
    """Satisfy this attempt from an identical verified run, if one exists.

    Returns ``(prepared_base_ref, exit_code)``. ``exit_code`` is set only when
    a prior run was reused. ``prepared_base_ref`` is the already-synced base, so
    a new run does not sync it twice.
    """

    try:
        prepared_base_ref = _pre_dispatch_sync_base_ref(
            target_repo=task.target_repo,
            base_ref=task.base_ref,
            remote_name=task.remote_name,
        )
    except PreDispatchGitSyncError:
        # This sync only establishes duplicate evidence. The new run that
        # follows syncs again and records any failure in its own run directory.
        return None, None
    source_base_sha = _resolve_source_base_sha(task.target_repo, prepared_base_ref)
    if source_base_sha is None:
        return prepared_base_ref, None
    prior_success = _prior_successful_run(
        runs_root=runs_root,
        task=task,
        source_base_sha=source_base_sha,
        rendered_prompt=prompt,
    )
    if prior_success is None:
        return prepared_base_ref, None
    # The base must not have moved while the run history was being read.
    if _resolve_source_base_sha(task.target_repo, prepared_base_ref) != source_base_sha:
        return prepared_base_ref, None
    prior_run, prior_summary = prior_success
    print(
        "[atlas-dispatch] DUPLICATE RUN SUPPRESSED: "
        f"task={task.id} prior_run_dir={prior_run.resolve()} "
        f"base_ref={task.base_ref} "
        f"source_base_sha={source_base_sha}; no CLI was invoked"
    )
    return prepared_base_ref, _write_duplicate_satisfied_run(
        task=task,
        runs_root=runs_root,
        prior_run=prior_run,
        prior_summary=prior_summary,
        source_base_sha=source_base_sha,
    )


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
    _start_run_record(run_dir, task=task, prompt=prompt)

    worktree_base_ref = (
        prepared_base_ref
        if prepared_base_ref is not None
        else _pre_dispatch_sync_base_ref(
            target_repo=task.target_repo,
            base_ref=task.base_ref,
            remote_name=task.remote_name,
        )
    )
    source_base_sha = _resolve_source_base_sha(task.target_repo, worktree_base_ref)
    worktree = create_worktree(
        repo_root=task.target_repo,
        branch=task.worktree_branch,
        base_ref=worktree_base_ref,
        detach=task.detached_head,
        allow_destructive_branch_reset=task.allow_destructive_branch_reset,
        protected_head_sha=task.protected_head_sha,
    )
    print(f"[atlas-dispatch] worktree={worktree.worktree_path}")
    mcp_runtime = _prepare_mcp_runtime(task.cli, worktree.worktree_path, task.mcp_servers)
    invocation, command = _resolve_cli_command(
        task, worktree=worktree, prompt=prompt, mcp_runtime=mcp_runtime
    )

    progress.cli_invoked = True
    cli_result, no_change, ref_guard = _run_cli_under_ref_guard(
        task,
        run_dir=run_dir,
        worktree=worktree,
        base_ref=worktree_base_ref,
        prompt=prompt,
        command=command,
        mcp_runtime=mcp_runtime,
    )
    _persist_cli_result(
        run_dir,
        cli_result,
        task=task,
        review_model_evidence=invocation.review_model_evidence.to_dict(),
        orchestrator_ref_guard=ref_guard,
    )
    _print_cli_outcome(task, cli_result)

    changed, verified_head_sha = _commit_and_record_changes(
        task,
        run_dir=run_dir,
        worktree=worktree,
        base_ref=worktree_base_ref,
        no_change=no_change,
    )
    cli_succeeded = _cli_succeeded(cli_result)
    # Publication is preservation, so it happens before post-run verification.
    # A crash or kill during a long acceptance suite must not leave the
    # completed head reachable only from one machine's disk.
    branch_publish = _publish_branch_if_requested(
        run_dir=run_dir,
        task=task,
        worktree=worktree,
        built_sha=verified_head_sha,
    )
    report, post_run = _verify_under_heartbeat(
        task,
        run_dir=run_dir,
        worktree=worktree,
        base_ref=worktree_base_ref,
        changed=changed,
        cli_succeeded=cli_succeeded,
        heartbeat=heartbeat,
    )

    return _finish_run(
        task,
        run_dir=run_dir,
        worktree=worktree,
        cli_result=cli_result,
        report=report,
        changed=changed,
        verified_head_sha=verified_head_sha,
        source_base_sha=source_base_sha,
        post_run=post_run,
        ref_guard=ref_guard,
        branch_publish=branch_publish,
        cli_succeeded=cli_succeeded,
    )


def _finish_run(
    task: TaskSpec,
    *,
    run_dir: Path,
    worktree: Worktree,
    cli_result: AdapterResult,
    report: VerifyReport,
    changed: list[str],
    verified_head_sha: str,
    source_base_sha: str | None,
    post_run: dict[str, object],
    ref_guard: dict[str, object],
    branch_publish: BranchPublishResult | None,
    cli_succeeded: bool,
) -> int:
    """Persist the verdict, write the report, and return the run's exit code."""

    ref_guard_passed = ref_guard["passed"] is True
    _persist_verification_summary(
        run_dir,
        report,
        verified_head_sha=verified_head_sha,
        source_base_sha=source_base_sha,
        post_run=post_run,
        ref_guard_passed=ref_guard_passed,
    )
    _write_report(
        run_dir,
        task=task,
        cli_result=cli_result,
        report=report,
        changed=changed,
        worktree_path=worktree.worktree_path,
        branch_publish=branch_publish,
        post_run=post_run,
        orchestrator_ref_guard=ref_guard,
    )
    verify_passed = report.passed and ref_guard_passed
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


def _start_run_record(run_dir: Path, *, task: TaskSpec, prompt: str) -> None:
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (run_dir / "task.json").write_text(
        json.dumps(asdict(task), indent=2, default=str), encoding="utf-8"
    )
    print(f"[atlas-dispatch] task={task.id} cli={task.cli} resolved_via={task.resolved_via}")
    print(
        f"[atlas-dispatch] model={task.model or '(default)'} "
        f"reasoning={task.reasoning_effort or '(default)'}"
    )
    print(f"[atlas-dispatch] target_repo={task.target_repo}")
    print(f"[atlas-dispatch] branch={task.worktree_branch}")
    print(f"[atlas-dispatch] run_dir={run_dir}")


def _resolve_cli_command(
    task: TaskSpec,
    *,
    worktree: Worktree,
    prompt: str,
    mcp_runtime: McpRuntime | None,
) -> tuple[ResolvedInvocation, list[str]]:
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
        command = inject_mcp_config(task.cli, command, mcp_runtime.config_path)
    return invocation, command


def _run_cli_under_ref_guard(
    task: TaskSpec,
    *,
    run_dir: Path,
    worktree: Worktree,
    base_ref: str,
    prompt: str,
    command: list[str],
    mcp_runtime: McpRuntime | None,
) -> tuple[AdapterResult, bool, dict[str, object]]:
    """Run the CLI; return its result, whether it changed nothing, and the ref guard."""

    # Recorded BEFORE the CLI runs, because it is the only thing that can tell
    # "the agent did nothing" from "the agent's work was absorbed into base_ref".
    # See _should_classify_no_change.
    head_before_cli = _worktree_head_sha(worktree.worktree_path)
    # A worktree shares one .git with the orchestrator, so the agent can move
    # refs the allowlist cannot see. Snapshot them; compare after.
    refs_before = _capture_orchestrator_refs(task.target_repo)
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
    refs_after = _capture_orchestrator_refs(task.target_repo)
    ref_guard = _record_orchestrator_ref_guard(
        run_dir=run_dir,
        repo=task.target_repo,
        before=refs_before,
        after=refs_after,
    )

    no_change = _should_classify_no_change(
        worktree=worktree,
        cli_result=cli_result,
        base_ref=base_ref,
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
    return cli_result, no_change, ref_guard


def _print_cli_outcome(task: TaskSpec, cli_result: AdapterResult) -> None:
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


def _cli_succeeded(cli_result: AdapterResult) -> bool:
    classification = cli_result.classification
    return (
        cli_result.exit_code == 0
        and not cli_result.timed_out
        and (classification is None or classification.kind == DispatchErrorKind.SUCCESS)
    )


def _commit_and_record_changes(
    task: TaskSpec,
    *,
    run_dir: Path,
    worktree: Worktree,
    base_ref: str,
    no_change: bool,
) -> tuple[list[str], str]:
    """Auto-commit what the agent left (ADR-005); return changed files and HEAD."""

    auto_committed = (
        False
        if no_change
        else commit_all(worktree, message=f"{task.cli}({task.id}): {task.title}")
    )
    print(f"[atlas-dispatch] auto_committed={auto_committed}")

    verified_head_sha = _resolve_verified_head_sha(worktree.worktree_path)
    changed = list_changed_files(worktree, base_ref=base_ref)
    changed_evidence = _known_changed_files_evidence(changed)
    _write_changed_files_evidence(run_dir, changed_evidence)
    _persist_changed_files_summary(run_dir, changed_evidence)
    return changed, verified_head_sha


def _verify_under_heartbeat(
    task: TaskSpec,
    *,
    run_dir: Path,
    worktree: Worktree,
    base_ref: str,
    changed: list[str],
    cli_succeeded: bool,
    heartbeat: Any | None,
) -> tuple[VerifyReport, dict[str, object]]:
    """Run the post-run checks; a crash here leaves an explicit incomplete record."""

    post_run_heartbeat = _PostRunHeartbeat(
        run_dir=run_dir,
        task=task,
        worktree_path=worktree.worktree_path,
        heartbeat=heartbeat,
    )
    try:
        _persist_post_run_started(
            run_dir=run_dir,
            task=task,
            post_run=post_run_heartbeat.start(),
        )
        report = _verify_committed_result(
            task,
            run_dir=run_dir,
            worktree=worktree,
            changed=changed,
            cli_succeeded=cli_succeeded,
        )
        _write_combined_diff_artifact(
            run_dir=run_dir,
            task=task,
            worktree=worktree,
            base_ref=base_ref,
        )
    except BaseException as exc:
        _persist_post_run_incomplete(
            run_dir=run_dir,
            task=task,
            post_run=post_run_heartbeat.finish(state="incomplete", error=exc),
            error=exc,
        )
        raise
    return report, post_run_heartbeat.finish(state="completed")


def _verify_committed_result(
    task: TaskSpec,
    *,
    run_dir: Path,
    worktree: Worktree,
    changed: list[str],
    cli_succeeded: bool,
) -> VerifyReport:
    allow_passed, forbidden_violations, out_of_scope = check_allowlist(
        changed_files=changed,
        allowed_paths=task.allowed_paths,
        forbidden_paths=task.forbidden_paths,
    )
    protected_path_violations = check_protected_paths(changed_files=changed)

    command_results: list[CheckResult] = []
    if cli_succeeded and changed:
        # The collection floor is passed only when the spec declares one.
        floor: dict[str, Any] = (
            {"acceptance_min_collected": task.acceptance_min_collected}
            if task.acceptance_min_collected is not None
            else {}
        )
        command_results = run_acceptance_commands(
            commands=task.acceptance,
            cwd=worktree.worktree_path,
            timeout_seconds=task.acceptance_timeout_seconds,
            import_provenance_required=_acceptance_requires_import_provenance(
                task.acceptance,
                enabled=task.check_import_provenance,
            ),
            run_dir=run_dir,
            **floor,
        )

    return VerifyReport(
        changed_files_present=bool(changed),
        allowlist_passed=allow_passed,
        forbidden_violations=forbidden_violations,
        out_of_scope_paths=out_of_scope,
        protected_path_violations=protected_path_violations,
        command_results=command_results,
        acceptance_skip_reason=(
            None
            if command_results
            else _acceptance_skip_reasons(
                task,
                cli_succeeded=cli_succeeded,
                changed=changed,
                allowlist_passed=allow_passed,
            )
        ),
    )


def _acceptance_skip_reasons(
    task: TaskSpec,
    *,
    cli_succeeded: bool,
    changed: list[str],
    allowlist_passed: bool,
) -> list[str]:
    """Why acceptance did not run. Every reason that applies is listed."""

    reasons: list[str] = []
    if not cli_succeeded:
        reasons.append("cli_failed")
    if not changed:
        reasons.append("no_files_changed")
    if not allowlist_passed:
        reasons.append("allowlist_failed")
    if not task.acceptance:
        # A spec that declares NO acceptance commands was not gated. That is a
        # different fact from "we ran the gate and it failed", and from
        # "something went wrong and we cannot say", so it gets its own reason.
        #
        # verify_passed deliberately stays False. Not gated is not passed, and
        # a fallback that turns an absent check into a green one makes every
        # real failure unreadable.
        reasons.append("no_acceptance_configured")
    return reasons or ["unknown"]


def _acceptance_requires_import_provenance(
    commands: list[str], *, enabled: bool = True
) -> bool:
    if not enabled:
        return False
    return any(
        import_provenance.is_import_bearing_acceptance_command(command)
        for command in commands
    )


def _cli_run_extra_env(
    *,
    run_dir: Path,
    mcp_runtime: McpRuntime | None,
) -> dict[str, str]:
    """Return internal overrides for only the dispatched CLI subprocess."""
    extra_env = dict(mcp_runtime.extra_env or {}) if mcp_runtime is not None else {}
    extra_env["UV_CACHE_DIR"] = str((run_dir / "uv-cache").resolve())
    return extra_env


def _publish_branch_if_requested(
    *,
    run_dir: Path,
    task: TaskSpec,
    worktree: Worktree,
    built_sha: str = "",
) -> BranchPublishResult | None:
    """Push the task branch when the spec sets ``push_branch``, whatever the outcome.

    PUBLICATION IS PRESERVATION, NOT APPROVAL, so this deliberately does not
    take the verification result. Refusing to push a failed run inverts the
    risk: the work most likely to be lost (red, partial, needs another look) is
    exactly the work that would not be preserved. It would also block
    cross-model review, since a reviewer task uses base_ref=origin/<branch> and
    cannot start from a branch that is not on the remote.

    Pushing a branch is not merging it; the harness never merges. A pushed
    branch is not a verified one either: the verdict stays in the run report
    and cli.summary.json.
    """
    if not task.push_branch:
        return None
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
