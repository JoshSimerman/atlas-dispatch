"""Bring the base branch up to date before a task's worktree is created.

For a local ``main`` this is ``checkout`` + ``fetch`` + ``merge --ff-only``; it
refuses (never merges or resets) when local and remote have diverged. Other
base refs are resolved to a local branch or fetched as a remote-tracking ref.
Index-lock contention and brief connection failures are retried a few times.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from atlas_dispatch.git_exec import (
    PREDISPATCH_GIT_LOCK_CONTENTION_KIND,
    PREDISPATCH_GIT_LOCK_CONTENTION_SUGGESTED_ACTION,
    git_result_error,
    git_result_is_index_lock_contention,
    pre_dispatch_repo_lock,
    run_git_in,
    run_pre_dispatch_git_with_index_lock_retry,
)

PREDISPATCH_GIT_FETCH_CONNECTION_RETRY_DELAYS_SECONDS = (0.1, 0.2)

GitCall = Callable[[list[str]], subprocess.CompletedProcess[str]]


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


def _pre_dispatch_sync_base_ref(
    *,
    target_repo: Path,
    base_ref: str,
    remote_name: str = "origin",
    sleep_fn: Callable[[float], None] | None = None,
) -> str:
    """Fast-forward a local base branch before dispatch creates a worktree.

    Returns the ref the worktree should be cut from.
    """

    base_ref = base_ref.strip() or "main"
    remote_name = remote_name.strip() or "origin"

    if not target_repo.is_dir() or not (target_repo / ".git").exists():
        return base_ref

    if _base_ref_is_remote_or_direct_ref(base_ref, remote_name=remote_name):
        return base_ref

    sleep = time.sleep if sleep_fn is None else sleep_fn

    def git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return run_pre_dispatch_git_with_index_lock_retry(
            lambda git_args: run_git_in(target_repo, git_args),
            args,
            sleep_fn=sleep,
        )

    def fetch_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return _run_pre_dispatch_fetch(git, args, sleep_fn=sleep)

    with pre_dispatch_repo_lock(target_repo):
        if base_ref != "main":
            return _resolve_non_main_base_ref(
                target_repo=target_repo,
                base_ref=base_ref,
                remote_name=remote_name,
                git=git,
                fetch_git=fetch_git,
            )
        return _fast_forward_local_base_branch(
            target_repo=target_repo,
            base_ref=base_ref,
            remote_name=remote_name,
            git=git,
            fetch_git=fetch_git,
        )


def _resolve_non_main_base_ref(
    *,
    target_repo: Path,
    base_ref: str,
    remote_name: str,
    git: GitCall,
    fetch_git: GitCall,
) -> str:
    """Use a local branch as-is, or fetch the remote branch and cut from that."""

    resolved_base_ref = _worktree_base_ref(
        base_ref,
        remote_name=remote_name,
        target_repo=target_repo,
    )
    if resolved_base_ref == base_ref:
        return resolved_base_ref
    fetch_refspec = f"{base_ref}:refs/remotes/{remote_name}/{base_ref}"
    if not _fetch_unless_remote_missing(
        fetch_git,
        ["fetch", remote_name, fetch_refspec],
        target_repo=target_repo,
        base_ref=base_ref,
        remote_name=remote_name,
    ):
        return resolved_base_ref
    _require_git_success(
        git(["rev-parse", resolved_base_ref]),
        target_repo=target_repo,
        base_ref=base_ref,
        action=f"resolve {resolved_base_ref}",
        failure=f"Could not resolve {resolved_base_ref} after fetch on {target_repo}",
    )
    return resolved_base_ref


def _fast_forward_local_base_branch(
    *,
    target_repo: Path,
    base_ref: str,
    remote_name: str,
    git: GitCall,
    fetch_git: GitCall,
) -> str:
    """Check out the local base branch and fast-forward it to the remote."""

    _require_git_success(
        git(["checkout", base_ref]),
        target_repo=target_repo,
        base_ref=base_ref,
        action="checkout local",
        failure=f"Could not checkout local {base_ref} on {target_repo} before dispatch",
    )
    if not _fetch_unless_remote_missing(
        fetch_git,
        ["fetch", remote_name, base_ref],
        target_repo=target_repo,
        base_ref=base_ref,
        remote_name=remote_name,
    ):
        return base_ref

    remote_ref = f"{remote_name}/{base_ref}"
    remote_rev = _require_git_success(
        git(["rev-parse", remote_ref]),
        target_repo=target_repo,
        base_ref=base_ref,
        action=f"resolve {remote_ref}",
        failure=f"Could not resolve {remote_ref} after fetch on {target_repo}",
    )
    head_rev = _require_git_success(
        git(["rev-parse", "HEAD"]),
        target_repo=target_repo,
        base_ref=base_ref,
        action="resolve HEAD",
        failure=f"Could not resolve HEAD on {target_repo} before dispatch",
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
    raise _fast_forward_refusal(
        git,
        target_repo=target_repo,
        base_ref=base_ref,
        remote_ref=remote_ref,
        merge=merge,
    )


def _fast_forward_refusal(
    git: GitCall,
    *,
    target_repo: Path,
    base_ref: str,
    remote_ref: str,
    merge: subprocess.CompletedProcess[str],
) -> PreDispatchGitSyncError:
    """Explain why ``merge --ff-only`` failed. Never merges or resets."""

    rev_list = git(["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"])
    try:
        ahead_str, behind_str = rev_list.stdout.strip().split("\t")
        ahead = int(ahead_str)
        behind = int(behind_str)
    except Exception:
        ahead = behind = 0

    if ahead > 0 and behind > 0:
        return PreDispatchGitSyncError(
            f"Local {base_ref} has DIVERGED from {remote_ref}: ahead={ahead}, "
            f"behind={behind}. Manual reconciliation required before dispatch."
        )
    if ahead > 0:
        return PreDispatchGitSyncError(
            f"Local {base_ref} is AHEAD of {remote_ref} by {ahead} commit(s); "
            "manual intervention required before dispatch."
        )
    if behind > 0:
        return PreDispatchGitSyncError(
            f"Local {base_ref} is behind {remote_ref} by {behind} commit(s), "
            f"but fast-forward failed. Run: git -C {target_repo} merge --ff-only {remote_ref}"
        )
    return PreDispatchGitSyncError(
        f"Local {base_ref} diverged from {remote_ref} on {target_repo}; "
        f"manual intervention required: {git_result_error(merge)}"
    )


def _fetch_unless_remote_missing(
    fetch_git: GitCall,
    args: list[str],
    *,
    target_repo: Path,
    base_ref: str,
    remote_name: str,
) -> bool:
    """Fetch; return False when the remote does not exist (a local-only repo)."""

    fetch = fetch_git(args)
    if fetch.returncode == 0:
        return True
    if _git_result_is_missing_remote(fetch, remote_name):
        return False
    _require_git_success(
        fetch,
        target_repo=target_repo,
        base_ref=base_ref,
        action=f"fetch {remote_name}/{base_ref}",
        failure=(
            f"Could not fetch {remote_name}/{base_ref} for {target_repo} before dispatch"
        ),
    )
    return True


def _require_git_success(
    result: subprocess.CompletedProcess[str],
    *,
    target_repo: Path,
    base_ref: str,
    action: str,
    failure: str,
) -> subprocess.CompletedProcess[str]:
    """Return a successful result; raise a classified error for a failed one."""

    if result.returncode == 0:
        return result
    _raise_if_pre_dispatch_git_lock_contention(
        target_repo=target_repo,
        base_ref=base_ref,
        action=action,
        result=result,
    )
    raise PreDispatchGitSyncError(
        f"{failure}; manual intervention required: {git_result_error(result)}"
    )


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


def _raise_if_pre_dispatch_git_lock_contention(
    *,
    target_repo: Path,
    base_ref: str,
    action: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    if git_result_is_index_lock_contention(result):
        raise PreDispatchGitSyncError(
            f"Transient pre-dispatch git index lock contention "
            f"({PREDISPATCH_GIT_LOCK_CONTENTION_KIND}) while trying to {action} "
            f"{base_ref} on {target_repo}; retry or redispatch. "
            f"Git output: {git_result_error(result)}",
            classification=PREDISPATCH_GIT_LOCK_CONTENTION_KIND,
            transient=True,
            suggested_action=PREDISPATCH_GIT_LOCK_CONTENTION_SUGGESTED_ACTION,
        )


def _run_pre_dispatch_fetch(
    git: GitCall,
    args: list[str],
    *,
    sleep_fn: Callable[[float], None],
) -> subprocess.CompletedProcess[str]:
    delays = PREDISPATCH_GIT_FETCH_CONNECTION_RETRY_DELAYS_SECONDS
    for attempt_index in range(len(delays) + 1):
        result = git(args)
        if not _git_result_is_transient_connection_error(result):
            return result
        if attempt_index >= len(delays):
            return result
        sleep_fn(delays[attempt_index])
    return result


def _base_ref_is_remote_or_direct_ref(base_ref: str, *, remote_name: str) -> bool:
    if base_ref.startswith("refs/"):
        return True
    if base_ref.startswith(f"{remote_name}/"):
        return True
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", base_ref))


def _git_ref_resolves(target_repo: Path, ref: str) -> bool:
    result = run_git_in(target_repo, ["rev-parse", "--verify", ref])
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
