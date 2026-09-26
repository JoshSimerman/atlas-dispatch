"""Facts about a task worktree's HEAD, read before and after the CLI runs."""

from __future__ import annotations

import re
from pathlib import Path

from atlas_dispatch.adapter import AdapterResult
from atlas_dispatch.git_exec import run_git_in
from atlas_dispatch.worktree import Worktree

NO_CHANGE_SUGGESTED_ACTION = (
    "CLI returned 0 exit code but produced no commits. Likely cause: auth "
    "failure, model refusal, or context-window pre-emption. Verify CLI auth, "
    "read cli.stdout.txt for the model's own explanation, then redispatch."
)


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


def _worktree_head_sha(worktree_path: Path) -> str | None:
    result = run_git_in(worktree_path, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _commits_ahead_of_base(worktree_path: Path, *, base_ref: str) -> int | None:
    result = run_git_in(worktree_path, ["rev-list", "--count", f"{base_ref}..HEAD"])
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _has_uncommitted_work(worktree_path: Path) -> bool:
    result = run_git_in(worktree_path, ["status", "--porcelain"])
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _resolve_verified_head_sha(worktree_path: Path) -> str:
    result = run_git_in(worktree_path, ["rev-parse", "--verify", "HEAD^{commit}"])
    head_sha = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None:
        detail = result.stderr.strip() or head_sha or f"exit {result.returncode}"
        raise RuntimeError(f"could not resolve verified worktree HEAD: {detail}")
    return head_sha
