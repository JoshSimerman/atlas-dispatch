"""Git worktree helpers.

Each task runs in its own git worktree on its own branch so parallel
dispatches do not collide on the working tree, and a reviewer can
inspect the diff before anyone merges it. The dispatcher creates the worktree
before the CLI run and leaves it intact afterwards so the human reviewer
(or a follow-up CLI run) can inspect or amend.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from atlas_dispatch.git_exec import run_git as _run_git

LOGGER = logging.getLogger(__name__)

_AUTO_CLEAN_REASON = "stale_dir_or_branch_lock"
_VENV_REQUIREMENT_BATCH_SIZE = 50
_FIRST_PARTY_UV_CACHE_DIRNAME = ".atlas-dispatch-first-party-uv-cache"
_MAX_PUBLISH_FALLBACK_RUN = 10_000


def run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git and preserve checked-call semantics for watchdog timeouts."""
    result = _run_git(
        args,
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
        runner=runner,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


class WorktreeVenvError(RuntimeError):
    """Raised when atlas-dispatch cannot create an isolated worktree venv."""


@dataclass(frozen=True, kw_only=True)
class Worktree:
    repo_root: Path
    worktree_path: Path
    branch: str


@dataclass(frozen=True, kw_only=True)
class BranchPublishResult:
    remote_name: str
    requested_remote_ref: str
    remote_ref: str
    pushed: bool
    state: str
    return_code: int
    built_sha: str = ""
    remote_head_sha: str = ""
    verification_state: str = "not_attempted"
    verification_detail: str = ""
    stdout: str = ""
    stderr: str = ""

    @property
    def error(self) -> str:
        if self.pushed:
            return ""
        return self.verification_detail or (self.stderr or self.stdout).strip()

    @property
    def reason(self) -> str:
        if self.state == "published":
            return ""
        return self.error


@dataclass(frozen=True, kw_only=True)
class BranchRescueRef:
    ref: str
    sha: str
    created_at: str
    branch: str
    host: str
    artifact_path: str = ""

    def metadata(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "sha": self.sha,
            "created_at": self.created_at,
            "branch": self.branch,
        }


@dataclass(frozen=True, kw_only=True)
class RemoteContainmentResult:
    """Remote-authoritative proof that one advertised ref contains a build."""

    state: str
    built_sha: str
    remote_name: str
    remote_ref: str
    remote_head_sha: str = ""
    detail: str = ""

    @property
    def verified_present(self) -> bool:
        return self.state == "verified_present"

    def metadata(self) -> dict[str, str]:
        return {
            "state": self.state,
            "built_sha": self.built_sha,
            "remote_name": self.remote_name,
            "remote_ref": self.remote_ref,
            "remote_head_sha": self.remote_head_sha,
            "detail": self.detail,
        }


@dataclass(frozen=True, kw_only=True)
class _WorktreeVenvPlan:
    uv_path: Path
    parent_python: Path
    dependency_specs: tuple[str, ...]
    editable_specs: tuple[str, ...]


def worktree_dir_name(repo_name: str, branch: str) -> str:
    """Return the canonical worktree directory name for a repo and branch."""
    slug = branch.replace("/", "-")
    digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:8]
    return f"{repo_name}-wt-{slug}-{digest}"


def worktree_path_for(
    repo_root: Path, branch: str, parent: Path | None = None
) -> Path:
    """Return the canonical, unresolved worktree path for a repo and branch."""
    worktree_parent = parent if parent is not None else repo_root.parent
    return worktree_parent / worktree_dir_name(repo_root.name, branch)


def create_worktree(
    *,
    repo_root: Path,
    branch: str,
    base_ref: str = "main",
    parent_dir: Path | None = None,
    detach: bool = False,
    allow_destructive_branch_reset: bool = False,
    protected_head_sha: str | None = None,
) -> Worktree:
    """Create a new worktree at <parent>/<repo-name>-wt-<branch-slug>-<hash>.

    If detach is true, create a detached-HEAD worktree from base_ref and use
    branch only as the stable path label. Otherwise, if the branch already
    exists, the worktree is created on the existing branch. Stale clean
    worktrees using the target path or branch are removed first so redispatches
    do not fail on leftover worktree state. The creation lock only covers this
    setup window; callers must serialize overlapping dispatches to the same
    branch for the full worktree lifetime. Reused branches with commits missing
    from base_ref and all remotes are rescued and refused unless
    allow_destructive_branch_reset is explicitly true. A reused branch whose
    current head equals protected_head_sha is always refused because resetting
    it would detach recorded review evidence from the branch it describes.
    """
    worktree_path = worktree_path_for(repo_root, branch, parent=parent_dir).resolve()

    with _worktree_create_lock(repo_root):
        if worktree_path.exists() and not worktree_path.is_dir():
            raise ValueError(
                f"worktree path already exists and is not a directory: {worktree_path}"
            )

        if worktree_path.is_dir():
            _auto_remove_worktree(
                repo_root,
                worktree_path,
                branch=branch,
                base_ref=base_ref,
            )

        for branch_worktree in _worktree_paths_for_branch(repo_root, branch):
            if branch_worktree != worktree_path or not branch_worktree.exists():
                _auto_remove_worktree(
                    repo_root,
                    branch_worktree,
                    branch=branch,
                    base_ref=base_ref,
                )

        _ensure_ref_available(repo_root, base_ref)
        if detach:
            cmd = ["worktree", "add", "--detach", str(worktree_path), base_ref]
        elif _branch_exists(repo_root, branch):
            _reset_existing_branch_for_reuse(
                repo_root=repo_root,
                branch=branch,
                base_ref=base_ref,
                allow_destructive_branch_reset=allow_destructive_branch_reset,
                protected_head_sha=protected_head_sha,
            )
            cmd = ["worktree", "add", str(worktree_path), branch]
        else:
            cmd = ["worktree", "add", "-b", branch, str(worktree_path), base_ref]

        run_git(cmd, cwd=repo_root, check=True)
        _assert_worktree_descends_from_base(worktree_path, base_ref)
        _create_isolated_worktree_venv(repo_root, worktree_path)
    return Worktree(repo_root=repo_root, worktree_path=worktree_path, branch=branch)


def remove_worktree(worktree: Worktree, *, force: bool = False) -> None:
    """Remove a worktree. Branch is left intact."""
    cmd = ["worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(worktree.worktree_path))
    run_git(cmd, cwd=worktree.repo_root, check=True)


def list_changed_files(worktree: Worktree, *, base_ref: str = "main") -> list[str]:
    """Return paths changed in the worktree relative to base_ref (POSIX-style).

    ``--no-renames`` is REQUIRED, not stylistic. Git detects renames by
    default, and a detected rename records ONLY the post-rename path. The
    protected-path check measures the paths it is handed, so a high-similarity
    rename of a protected file would silently escape it:

        configs/prod.yaml -> docs/notes.md
            recorded ['docs/notes.md']  -> no protected path touched

    With ``--no-renames`` both endpoints appear, so the pre-rename path still
    matches its rule. This trades a longer list for a gate that cannot be
    escaped by renaming the file it guards.
    """
    result = run_git(
        ["diff", "--name-only", "--no-renames", f"{base_ref}...HEAD"],
        cwd=worktree.worktree_path,
        check=True,
    )
    stdout = _require_git_text_output(result, operation="list changed files")
    paths = [line.strip() for line in stdout.splitlines()]
    if any(not path for path in paths) or "\x00" in stdout:
        raise ValueError("git diff returned unparseable changed-file output")
    return paths


def has_uncommitted_changes(worktree: Worktree) -> bool:
    """Return True if the worktree has staged, unstaged, or untracked changes."""
    result = run_git(
        ["status", "--porcelain"],
        cwd=worktree.worktree_path,
        check=True,
    )
    stdout = _require_git_text_output(result, operation="inspect worktree status")
    return stdout != ""


def commit_all(worktree: Worktree, *, message: str) -> bool:
    """Stage everything (`git add -A`) and commit if anything changed.

    Returns True if a commit was made. The dispatcher uses this to ensure
    the dispatched CLI's working-tree changes become a visible commit on
    the task branch before verify runs.
    """
    if not has_uncommitted_changes(worktree):
        return False

    run_git(
        ["add", "-A"],
        cwd=worktree.worktree_path,
        check=True,
    )
    run_git(
        ["commit", "-m", message],
        cwd=worktree.worktree_path,
        check=True,
    )
    return True


def verify_remote_ref_contains_sha(
    *,
    repo_root: Path,
    remote_name: str,
    remote_ref: str,
    built_sha: str,
) -> RemoteContainmentResult:
    """Prove by effect that an exact advertised remote ref contains ``built_sha``.

    Local tracking refs are deliberately ignored. The advertised ref is read with
    ``ls-remote`` and fetched into a disposable bare repository, where git's own
    ancestry predicate proves containment. A second ``ls-remote`` binds the fetched
    graph to the still-advertised ref and fails closed if the ref raced the proof.
    """

    remote_name = remote_name.strip()
    remote_ref = remote_ref.strip()
    built_sha = built_sha.strip().lower()
    if not repo_root.is_dir() or re.fullmatch(r"[0-9a-f]{40}", built_sha) is None:
        return RemoteContainmentResult(
            state="insufficient_evidence",
            built_sha=built_sha,
            remote_name=remote_name,
            remote_ref=remote_ref,
            detail="repository or full-40 built SHA is missing",
        )
    remote_branch = _remote_branch_from_ref(remote_name, remote_ref)
    if not remote_name or remote_branch is None:
        return RemoteContainmentResult(
            state="insufficient_evidence",
            built_sha=built_sha,
            remote_name=remote_name,
            remote_ref=remote_ref,
            detail="remote name or exact remote branch ref is missing or inconsistent",
        )

    remote_url_result = run_git(
        ["remote", "get-url", remote_name], cwd=repo_root, check=False, timeout=30
    )
    remote_url = (remote_url_result.stdout or "").strip()
    if remote_url_result.returncode != 0 or not remote_url:
        return RemoteContainmentResult(
            state="verification_unavailable",
            built_sha=built_sha,
            remote_name=remote_name,
            remote_ref=remote_ref,
            detail="configured remote URL could not be resolved",
        )

    advertised_ref = f"refs/heads/{remote_branch}"
    first = _ls_remote_head(
        repo_root=repo_root,
        remote_name=remote_name,
        advertised_ref=advertised_ref,
    )
    if first[0] != "verified_present":
        return RemoteContainmentResult(
            state=first[0],
            built_sha=built_sha,
            remote_name=remote_name,
            remote_ref=remote_ref,
            detail=first[2],
        )
    advertised_sha = first[1]

    try:
        with tempfile.TemporaryDirectory(prefix="atlas-dispatch-remote-proof-") as tmp:
            proof_repo = Path(tmp)
            initialized = run_git(["init", "--bare"], cwd=proof_repo, check=False)
            if initialized.returncode != 0:
                raise RuntimeError("could not initialize disposable proof repository")
            fetched = run_git(
                [
                    "-c",
                    "protocol.file.allow=always",
                    "fetch",
                    "--no-tags",
                    remote_url,
                    f"+{advertised_ref}:refs/heads/remote-proof",
                ],
                cwd=proof_repo,
                check=False,
                timeout=120,
            )
            if fetched.returncode != 0:
                return RemoteContainmentResult(
                    state="verification_unavailable",
                    built_sha=built_sha,
                    remote_name=remote_name,
                    remote_ref=remote_ref,
                    remote_head_sha=advertised_sha,
                    detail="advertised remote graph could not be fetched for containment proof",
                )
            fetched_head = run_git(
                ["rev-parse", "--verify", "refs/heads/remote-proof^{commit}"],
                cwd=proof_repo,
                check=False,
            )
            fetched_sha = (fetched_head.stdout or "").strip().lower()
            if (
                fetched_head.returncode != 0
                or re.fullmatch(r"[0-9a-f]{40}", fetched_sha) is None
            ):
                return RemoteContainmentResult(
                    state="verification_unavailable",
                    built_sha=built_sha,
                    remote_name=remote_name,
                    remote_ref=remote_ref,
                    remote_head_sha=advertised_sha,
                    detail="fetched remote head could not be resolved",
                )

            final = _ls_remote_head(
                repo_root=repo_root,
                remote_name=remote_name,
                advertised_ref=advertised_ref,
            )
            if final[0] != "verified_present" or final[1] != fetched_sha:
                return RemoteContainmentResult(
                    state="verification_unavailable",
                    built_sha=built_sha,
                    remote_name=remote_name,
                    remote_ref=remote_ref,
                    remote_head_sha=final[1] or advertised_sha,
                    detail="remote ref changed while containment was being proved",
                )

            built_object = run_git(
                ["cat-file", "-e", f"{built_sha}^{{commit}}"],
                cwd=proof_repo,
                check=False,
            )
            if built_object.returncode != 0:
                return RemoteContainmentResult(
                    state="verified_absent",
                    built_sha=built_sha,
                    remote_name=remote_name,
                    remote_ref=remote_ref,
                    remote_head_sha=fetched_sha,
                    detail="advertised remote graph does not contain the built SHA object",
                )
            contains = run_git(
                ["merge-base", "--is-ancestor", built_sha, fetched_sha],
                cwd=proof_repo,
                check=False,
            )
            if contains.returncode == 0:
                return RemoteContainmentResult(
                    state="verified_present",
                    built_sha=built_sha,
                    remote_name=remote_name,
                    remote_ref=remote_ref,
                    remote_head_sha=fetched_sha,
                    detail="advertised remote head contains the built SHA",
                )
            if contains.returncode == 1:
                return RemoteContainmentResult(
                    state="verified_absent",
                    built_sha=built_sha,
                    remote_name=remote_name,
                    remote_ref=remote_ref,
                    remote_head_sha=fetched_sha,
                    detail="advertised remote head does not contain the built SHA",
                )
            return RemoteContainmentResult(
                state="verification_unavailable",
                built_sha=built_sha,
                remote_name=remote_name,
                remote_ref=remote_ref,
                remote_head_sha=fetched_sha,
                detail="git could not evaluate ancestry in the fetched remote graph",
            )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return RemoteContainmentResult(
            state="verification_unavailable",
            built_sha=built_sha,
            remote_name=remote_name,
            remote_ref=remote_ref,
            remote_head_sha=advertised_sha,
            detail="remote containment proof could not be completed",
        )


def _remote_branch_from_ref(remote_name: str, remote_ref: str) -> str | None:
    prefixes = (f"{remote_name}/", f"refs/remotes/{remote_name}/")
    if remote_ref.startswith("refs/heads/"):
        branch = remote_ref.removeprefix("refs/heads/")
    else:
        branch = next(
            (
                remote_ref.removeprefix(prefix)
                for prefix in prefixes
                if remote_ref.startswith(prefix)
            ),
            "",
        )
    if not branch or branch.startswith("/") or branch.endswith("/") or ".." in branch:
        return None
    return branch


def _ls_remote_head(
    *, repo_root: Path, remote_name: str, advertised_ref: str
) -> tuple[str, str, str]:
    try:
        result = run_git(
            ["ls-remote", "--heads", remote_name, advertised_ref],
            cwd=repo_root,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "verification_unavailable", "", "remote advertisement was unreachable"
    if result.returncode != 0:
        return "verification_unavailable", "", "remote advertisement was unreachable"
    lines = [line.split() for line in (result.stdout or "").splitlines() if line.strip()]
    matches = [parts for parts in lines if len(parts) == 2 and parts[1] == advertised_ref]
    if not matches:
        return "verified_absent", "", "exact remote ref is not advertised"
    if len(matches) != 1 or re.fullmatch(r"[0-9a-fA-F]{40}", matches[0][0]) is None:
        return "verification_unavailable", "", "remote advertisement was ambiguous or malformed"
    return "verified_present", matches[0][0].lower(), ""


def publish_branch(
    worktree: Worktree,
    *,
    remote_name: str = "origin",
    built_sha: str = "",
) -> BranchPublishResult:
    """Publish a branch without overwriting an unrelated remote artifact.

    A reused local branch can have deliberately rewritten history while the
    requested remote branch still names a prior run. In that case a normal push
    is rejected, and publication continues at the first available deterministic
    ``-runN`` ref. Every attempt remains a non-force push, so a concurrent
    publisher can only make this publisher advance to the next suffix; it can
    never cause an existing remote artifact to be overwritten.
    """
    remote_name = remote_name.strip() or "origin"
    result = _publish_branch_to_remote(worktree, remote_name=remote_name)
    if not result.pushed:
        return result
    if not built_sha:
        # Compatibility for synthetic callers that do not point at a git
        # repository. The production dispatcher always supplies its resolved
        # full-40 build SHA, and real repositories still resolve HEAD here.
        if not (worktree.worktree_path / ".git").exists():
            return result
        head = run_git(
            ["rev-parse", "--verify", "HEAD^{commit}"],
            cwd=worktree.worktree_path,
            check=False,
        )
        built_sha = (head.stdout or "").strip()
    proof = verify_remote_ref_contains_sha(
        repo_root=worktree.worktree_path,
        remote_name=result.remote_name,
        remote_ref=result.remote_ref,
        built_sha=built_sha,
    )
    return replace(
        result,
        pushed=proof.verified_present,
        state="published" if proof.verified_present else "publish_failed",
        built_sha=proof.built_sha,
        remote_head_sha=proof.remote_head_sha,
        verification_state=proof.state,
        verification_detail=proof.detail,
    )


def _publish_branch_to_remote(
    worktree: Worktree, *, remote_name: str
) -> BranchPublishResult:
    result = _push_branch(worktree, remote_name=remote_name)
    if result.returncode == 0 or not _push_was_non_fast_forward(result):
        return _branch_publish_result(
            worktree,
            remote_name=remote_name,
            remote_branch=worktree.branch,
            result=result,
        )
    return _publish_branch_to_fallback_ref(
        worktree,
        remote_name=remote_name,
        initial_result=result,
    )


def _publish_branch_to_fallback_ref(
    worktree: Worktree,
    *,
    remote_name: str,
    initial_result: subprocess.CompletedProcess[str],
) -> BranchPublishResult:
    stdout_parts = [initial_result.stdout.strip()]
    stderr_parts = [initial_result.stderr.strip()]
    for run_number in range(2, _MAX_PUBLISH_FALLBACK_RUN + 1):
        remote_branch = f"{worktree.branch}-run{run_number}"
        attempt = _push_branch_to_ref(
            worktree,
            remote_name=remote_name,
            remote_branch=remote_branch,
        )
        stdout_parts.append(attempt.stdout.strip())
        stderr_parts.append(attempt.stderr.strip())
        if attempt.returncode == 0 or not _push_was_non_fast_forward(attempt):
            return _branch_publish_result(
                worktree,
                remote_name=remote_name,
                remote_branch=remote_branch,
                result=attempt,
                stdout=_joined_git_output(stdout_parts),
                stderr=_joined_git_output(stderr_parts),
            )

    exhausted = subprocess.CompletedProcess(
        args=["git", "push", remote_name, worktree.branch],
        returncode=1,
        stdout="",
        stderr=(
            "refused to overwrite any existing remote artifact and exhausted "
            f"deterministic fallback refs through -run{_MAX_PUBLISH_FALLBACK_RUN}"
        ),
    )
    stderr_parts.append(exhausted.stderr)
    return _branch_publish_result(
        worktree,
        remote_name=remote_name,
        remote_branch=f"{worktree.branch}-run{_MAX_PUBLISH_FALLBACK_RUN}",
        result=exhausted,
        stdout=_joined_git_output(stdout_parts),
        stderr=_joined_git_output(stderr_parts),
    )


def _push_branch(
    worktree: Worktree, *, remote_name: str
) -> subprocess.CompletedProcess[str]:
    return run_git(
        ["push", "-u", remote_name, worktree.branch],
        cwd=worktree.worktree_path,
        check=False,
    )


def _push_branch_to_ref(
    worktree: Worktree,
    *,
    remote_name: str,
    remote_branch: str,
) -> subprocess.CompletedProcess[str]:
    return run_git(
        ["push", "-u", remote_name, f"HEAD:refs/heads/{remote_branch}"],
        cwd=worktree.worktree_path,
        check=False,
    )


def _branch_publish_result(
    worktree: Worktree,
    *,
    remote_name: str,
    remote_branch: str,
    result: subprocess.CompletedProcess[str],
    stdout: str | None = None,
    stderr: str | None = None,
) -> BranchPublishResult:
    return BranchPublishResult(
        remote_name=remote_name,
        requested_remote_ref=f"{remote_name}/{worktree.branch}",
        remote_ref=f"{remote_name}/{remote_branch}",
        pushed=result.returncode == 0,
        state="published" if result.returncode == 0 else "publish_failed",
        return_code=result.returncode,
        stdout=result.stdout if stdout is None else stdout,
        stderr=result.stderr if stderr is None else stderr,
    )


def _joined_git_output(parts: object) -> str:
    return "\n".join(
        text
        for part in parts
        if (text := str(part or "").strip())
    )


def _push_was_non_fast_forward(
    result: subprocess.CompletedProcess[str],
) -> bool:
    if result.returncode == 0:
        return False
    output = f"{result.stdout}\n{result.stderr}".casefold()
    markers = (
        "non-fast-forward",
        "non fast-forward",
        "[rejected]",
        "fetch first",
    )
    return any(marker in output for marker in markers)


def is_git_repo(path: Path) -> bool:
    """True if `path` is the root of a git repository (or a worktree)."""
    if not path.is_dir():
        return False
    result = run_git(
        ["rev-parse", "--show-toplevel"],
        cwd=path,
        check=False,
    )
    if result.returncode != 0:
        return False
    toplevel = Path(result.stdout.strip()).resolve()
    return toplevel == path.resolve()


def _assert_worktree_descends_from_base(worktree_path: Path, base_ref: str) -> None:
    head = run_git(
        ["rev-parse", "HEAD"],
        cwd=worktree_path,
        check=True,
    ).stdout.strip()
    base = run_git(
        ["rev-parse", base_ref],
        cwd=worktree_path,
        check=True,
    ).stdout.strip()
    ancestry = run_git(
        ["merge-base", "--is-ancestor", base_ref, "HEAD"],
        cwd=worktree_path,
        check=False,
    )
    if ancestry.returncode != 0:
        raise ValueError(
            f"refusing stale-base worktree: HEAD {head} is not descended from "
            f"requested base_ref {base_ref} ({base})"
        )


def _branch_exists(repo_root: Path, branch: str) -> bool:
    result = run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
        check=False,
        capture_output=False,
    )
    return result.returncode == 0


def _reset_existing_branch_for_reuse(
    *,
    repo_root: Path,
    branch: str,
    base_ref: str,
    allow_destructive_branch_reset: bool,
    protected_head_sha: str | None = None,
) -> None:
    branch_head_sha = _branch_head_sha(repo_root=repo_root, branch=branch)
    if (
        protected_head_sha
        and branch_head_sha.casefold() == protected_head_sha.strip().casefold()
    ):
        LOGGER.error(
            "event=reviewed_branch_head_reset_refused branch=%s base_ref=%s "
            "protected_head_sha=%s",
            branch,
            base_ref,
            branch_head_sha,
            extra={
                "event": "reviewed_branch_head_reset_refused",
                "branch": branch,
                "base_ref": base_ref,
                "protected_head_sha": branch_head_sha,
            },
        )
        raise ValueError(
            _protected_head_reset_refusal_message(
                branch=branch,
                protected_head_sha=branch_head_sha,
            )
        )

    unpushed_commits = _branch_commits_missing_from_base_and_remotes(
        repo_root=repo_root,
        branch=branch,
        base_ref=base_ref,
    )
    if unpushed_commits:
        rescue = _create_branch_rescue_ref(repo_root=repo_root, branch=branch)
        rescue = _persist_branch_rescue_artifact(
            repo_root=repo_root,
            branch=branch,
            rescue=rescue,
        )
        if not allow_destructive_branch_reset:
            LOGGER.error(
                "event=branch_reset_refused branch=%s base_ref=%s rescue_ref=%s "
                "rescued_sha=%s host=%s unpushed_commits=%s",
                branch,
                base_ref,
                rescue.ref,
                rescue.sha,
                rescue.host,
                ",".join(unpushed_commits),
                extra={
                    "event": "branch_reset_refused",
                    "branch": branch,
                    "base_ref": base_ref,
                    "rescue_ref": rescue.ref,
                    "rescued_sha": rescue.sha,
                    "host": rescue.host,
                    "unpushed_commits": unpushed_commits,
                },
            )
            raise ValueError(_branch_reset_refusal_message(branch, base_ref, rescue))
        LOGGER.warning(
            "event=branch_reset_forced_after_rescue branch=%s base_ref=%s "
            "rescue_ref=%s rescued_sha=%s host=%s unpushed_commits=%s",
            branch,
            base_ref,
            rescue.ref,
            rescue.sha,
            rescue.host,
            ",".join(unpushed_commits),
            extra={
                "event": "branch_reset_forced_after_rescue",
                "branch": branch,
                "base_ref": base_ref,
                "rescue_ref": rescue.ref,
                "rescued_sha": rescue.sha,
                "host": rescue.host,
                "unpushed_commits": unpushed_commits,
            },
        )

    # Compare-and-swap the ref so a branch movement after the safety checks is
    # refused instead of being silently overwritten.
    base_sha = run_git(
        ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        cwd=repo_root,
        check=True,
    ).stdout.strip()
    reset = run_git(
        [
            "update-ref",
            "-m",
            "atlas-dispatch redispatch branch reset",
            f"refs/heads/{branch}",
            base_sha,
            branch_head_sha,
        ],
        cwd=repo_root,
        check=False,
    )
    if reset.returncode != 0:
        detail = str(reset.stderr or reset.stdout or "git update-ref failed").strip()
        raise ValueError(
            f"refusing to reset branch {branch!r}: the atomic ref update did not "
            f"complete ({detail}). The branch was not reset; re-run so its current "
            "head can be checked again."
        )


def _branch_head_sha(*, repo_root: Path, branch: str) -> str:
    result = run_git(
        ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
        cwd=repo_root,
        check=True,
    )
    commits = _parse_git_commit_lines(
        result,
        operation=f"resolve branch {branch!r} head",
    )
    if len(commits) != 1:
        raise ValueError(
            f"git returned no head while attempting to resolve branch {branch!r}"
        )
    return commits[0]


def _branch_commits_missing_from_base_and_remotes(
    *,
    repo_root: Path,
    branch: str,
    base_ref: str,
) -> list[str]:
    result = run_git(
        [
            "rev-list",
            "--max-count=20",
            f"refs/heads/{branch}",
            "--not",
            base_ref,
            "--remotes",
        ],
        cwd=repo_root,
        check=True,
    )
    return _parse_git_commit_lines(
        result,
        operation="inspect branch commits missing from base and remotes",
    )


def _create_branch_rescue_ref(
    *,
    repo_root: Path,
    branch: str,
    source_ref: str | None = None,
    source_cwd: Path | None = None,
    event: str = "branch_reset_rescue_created",
) -> BranchRescueRef:
    source_ref = source_ref or f"refs/heads/{branch}"
    tip_result = run_git(
        ["rev-parse", f"{source_ref}^{{commit}}"],
        cwd=source_cwd or repo_root,
        check=True,
    )
    tips = _parse_git_commit_lines(tip_result, operation="resolve rescue commit")
    if len(tips) != 1:
        raise ValueError("git rev-parse returned an unparseable rescue commit")
    tip = tips[0]
    created = datetime.now(UTC)
    timestamp = created.strftime("%Y%m%dT%H%M%S%fZ")
    rescue_ref = f"refs/rescue/{_rescue_branch_slug(branch)}/{timestamp}"
    run_git(
        ["update-ref", rescue_ref, tip],
        cwd=repo_root,
        check=True,
    )
    LOGGER.warning(
        "event=%s branch=%s rescue_ref=%s rescued_sha=%s",
        event,
        branch,
        rescue_ref,
        tip,
        extra={
            "event": event,
            "branch": branch,
            "rescue_ref": rescue_ref,
            "rescued_sha": tip,
        },
    )
    return BranchRescueRef(
        ref=rescue_ref,
        sha=tip,
        created_at=created.isoformat().replace("+00:00", "Z"),
        branch=branch,
        host=socket.gethostname(),
    )


def _persist_branch_rescue_artifact(
    *,
    repo_root: Path,
    branch: str,
    rescue: BranchRescueRef,
    run_dir: Path | None = None,
) -> BranchRescueRef:
    run_dir = run_dir or _latest_run_dir_for_branch(repo_root=repo_root, branch=branch)
    if run_dir is None:
        return rescue

    artifact_path = run_dir / "rescue_refs.json"
    try:
        existing = _read_rescue_refs_artifact(artifact_path)
        records = [
            record
            for record in existing
            if not (
                record.get("ref") == rescue.ref
                and record.get("sha") == rescue.sha
            )
        ]
        records.append(rescue.metadata())
        artifact_path.write_text(
            json.dumps(records, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.warning(
            "event=branch_reset_rescue_artifact_write_failed branch=%s "
            "rescue_ref=%s rescued_sha=%s artifact_path=%s error=%s",
            branch,
            rescue.ref,
            rescue.sha,
            artifact_path,
            exc,
            extra={
                "event": "branch_reset_rescue_artifact_write_failed",
                "branch": branch,
                "rescue_ref": rescue.ref,
                "rescued_sha": rescue.sha,
                "artifact_path": str(artifact_path),
            },
        )
        raise ValueError(
            _branch_reset_artifact_refusal_message(
                branch=branch,
                rescue=rescue,
                artifact_path=artifact_path,
                error=exc,
            )
        ) from exc
    return BranchRescueRef(
        ref=rescue.ref,
        sha=rescue.sha,
        created_at=rescue.created_at,
        branch=rescue.branch,
        host=rescue.host,
        artifact_path=str(artifact_path),
    )


def _read_rescue_refs_artifact(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    records: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        record = {
            "ref": str(item.get("ref") or ""),
            "sha": str(item.get("sha") or ""),
            "created_at": str(item.get("created_at") or ""),
            "branch": str(item.get("branch") or ""),
        }
        if record["ref"] and record["sha"]:
            records.append(record)
    return records


def _latest_run_dir_for_branch(*, repo_root: Path, branch: str) -> Path | None:
    runs_root = repo_root / ".atlas-dispatch" / "runs"
    if not runs_root.is_dir():
        return None
    candidates: list[tuple[float, str, Path]] = []
    for task_path in runs_root.glob("*/*/task.json"):
        if not _task_json_matches_branch(task_path, branch):
            continue
        try:
            stat = task_path.parent.stat()
        except OSError:
            continue
        candidates.append((stat.st_mtime, str(task_path.parent), task_path.parent))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _task_json_matches_branch(task_path: Path, branch: str) -> bool:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(raw, dict) and str(raw.get("worktree_branch") or "") == branch


def _branch_reset_refusal_message(
    branch: str, base_ref: str, rescue: BranchRescueRef
) -> str:
    parts = [
        (
            f"refusing to reset branch {branch!r}: it contains commits not "
            f"reachable from {base_ref!r} or any remote."
        ),
        f"Rescue ref: {rescue.ref}",
        f"Full sha: {rescue.sha}",
        f"Host: {rescue.host}",
        f"Inspect: git log {rescue.ref}",
        (
            "Proceed: set allow_destructive_branch_reset=True by adding "
            '"allow_destructive_branch_reset": true to the task spec, then re-run.'
        ),
    ]
    if rescue.artifact_path:
        parts.append(f"Rescue artifact: {rescue.artifact_path}")
    parts.append("The rescue ref remains as the audit trail after the reset.")
    return " ".join(parts)


def _protected_head_reset_refusal_message(
    *, branch: str, protected_head_sha: str
) -> str:
    return " ".join(
        [
            (
                f"refusing to reset branch {branch!r}: its current head "
                f"{protected_head_sha} is protected because a recorded review "
                "verdict describes that exact SHA."
            ),
            (
                "Resetting the branch would orphan the verdict from the branch "
                "state it describes even if the commit remains reachable on a remote."
            ),
            (
                "Remedy: repoint `worktree_branch` to a NEW branch name so the "
                "reviewed head is never touched, then re-run."
            ),
            (
                "`allow_destructive_branch_reset` does not override reviewed-head "
                "protection."
            ),
        ]
    )


def _branch_reset_artifact_refusal_message(
    *,
    branch: str,
    rescue: BranchRescueRef,
    artifact_path: Path,
    error: OSError,
) -> str:
    return " ".join(
        [
            (
                f"refusing to reset branch {branch!r}: rescue ref {rescue.ref} "
                f"was created at {rescue.sha}, but writing rescue artifact "
                f"{artifact_path} failed: {error}."
            ),
            "Fix write access to the run metadata path, then re-run.",
            "The branch was not reset.",
        ]
    )


def _rescue_branch_slug(branch: str) -> str:
    return branch.replace("/", "-")


@contextmanager
def _worktree_create_lock(repo_root: Path):
    lock_path = _git_common_dir(repo_root) / "atlas-dispatch-worktree-create.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _git_common_dir(repo_root: Path) -> Path:
    result = run_git(
        ["rev-parse", "--git-common-dir"],
        cwd=repo_root,
        check=False,
    )
    if result.returncode == 0:
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = repo_root / common_dir
        return common_dir.resolve()
    git_dir = repo_root / ".git"
    return git_dir if git_dir.is_dir() else repo_root


def _create_isolated_worktree_venv(repo_root: Path, worktree_path: Path) -> None:
    worktree_venv = worktree_path / ".venv"
    if worktree_venv.is_symlink():
        raise WorktreeVenvError(
            "worktree_venv_creation_failed: reason=target_venv_is_symlink "
            f"path={worktree_venv}"
        )
    if worktree_venv.exists():
        return

    plan = _worktree_venv_plan(repo_root, worktree_path)
    if plan is None:
        return
    try:
        _materialize_worktree_venv(plan, worktree_venv)
    except Exception:
        _remove_created_venv(worktree_venv)
        raise


def _worktree_venv_plan(
    repo_root: Path, worktree_path: Path
) -> _WorktreeVenvPlan | None:
    repo_root = repo_root.resolve()
    worktree_path = worktree_path.resolve()
    parent_venv = repo_root / ".venv"
    if not parent_venv.is_dir():
        return None

    parent_python = parent_venv / "bin" / "python"
    if not parent_python.is_file():
        raise WorktreeVenvError(
            "worktree_venv_creation_failed: reason=parent_python_missing "
            f"path={parent_python}"
        )

    uv_path = _find_uv()
    if uv_path is None:
        raise WorktreeVenvError("worktree_venv_creation_failed: reason=uv_not_found")

    freeze = _run_venv_command(
        [
            str(uv_path),
            "pip",
            "freeze",
            "--python",
            str(parent_python),
        ],
        cwd=repo_root,
        reason="parent_freeze_failed",
    )
    dependency_specs = _split_parent_requirements(
        repo_root=repo_root,
        parent_venv=parent_venv,
        freeze_output=freeze.stdout,
    )
    editable_specs = _discover_worktree_editable_specs(worktree_path)
    if not editable_specs:
        LOGGER.warning(
            "event=worktree_venv_editable_discovery_unknown "
            "worktree_path=%s reason=no_pyproject_toml_editable_roots",
            worktree_path,
            extra={
                "event": "worktree_venv_editable_discovery_unknown",
                "worktree_path": str(worktree_path),
                "reason": "no_pyproject_toml_editable_roots",
            },
        )
    return _WorktreeVenvPlan(
        uv_path=uv_path,
        parent_python=parent_python,
        dependency_specs=tuple(dependency_specs),
        editable_specs=tuple(editable_specs),
    )


def _materialize_worktree_venv(plan: _WorktreeVenvPlan, venv_path: Path) -> None:
    _run_venv_command(
        [
            str(plan.uv_path),
            "venv",
            "--python",
            str(plan.parent_python),
            str(venv_path),
        ],
        cwd=venv_path.parent,
        reason="uv_venv_failed",
    )
    worktree_python = venv_path / "bin" / "python"
    for batch in _batched(plan.dependency_specs, _VENV_REQUIREMENT_BATCH_SIZE):
        _run_venv_command(
            [
                str(plan.uv_path),
                "pip",
                "install",
                "--python",
                str(worktree_python),
                "--no-deps",
                *batch,
            ],
            cwd=venv_path.parent,
            reason="dependency_install_failed",
        )
    for editable_spec in plan.editable_specs:
        _run_venv_command(
            [
                str(plan.uv_path),
                "pip",
                "install",
                # Project name/version does not identify first-party source.
                # A fresh per-venv cache prevents shared-cache reuse, while
                # reinstall replaces a same-version wheel copied from the
                # parent environment. Dependency installs retain warm cache.
                "--cache-dir",
                str(venv_path / _FIRST_PARTY_UV_CACHE_DIRNAME),
                "--reinstall",
                "--python",
                str(worktree_python),
                "--no-deps",
                "-e",
                editable_spec,
            ],
            cwd=venv_path.parent,
            reason="editable_install_failed",
        )


def _remove_created_venv(venv_path: Path) -> None:
    if venv_path.is_symlink():
        venv_path.unlink()
    elif venv_path.exists():
        shutil.rmtree(venv_path, ignore_errors=True)


def _split_parent_requirements(
    *,
    repo_root: Path,
    parent_venv: Path,
    freeze_output: str,
) -> list[str]:
    dependency_specs: list[str] = []
    for raw_line in freeze_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("Using Python "):
            continue
        editable_spec = _editable_requirement_spec(line)
        if editable_spec is not None:
            _validate_parent_editable_requirement(
                repo_root=repo_root,
                parent_venv=parent_venv,
                editable_spec=editable_spec,
            )
            continue
        if _is_file_requirement(line):
            continue
        dependency_specs.append(line)
    return dependency_specs


def _discover_worktree_editable_specs(worktree_path: Path) -> list[str]:
    worktree = worktree_path.resolve()
    candidates: list[Path] = [worktree]
    candidates.extend(sorted((worktree / "libs").glob("*")))
    candidates.extend(sorted((worktree / "services").glob("*")))

    editable_specs: list[str] = []
    for candidate in candidates:
        if not (candidate / "pyproject.toml").is_file():
            continue
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(worktree)
        except ValueError:
            continue
        editable_specs.append(str(resolved))
    return editable_specs


def _validate_parent_editable_requirement(
    *,
    repo_root: Path,
    parent_venv: Path,
    editable_spec: str,
) -> None:
    editable_path = _requirement_file_path(editable_spec)
    if editable_path is None:
        return
    resolved_path = editable_path.resolve(strict=False)
    try:
        resolved_path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise WorktreeVenvError(
            "worktree_venv_creation_failed: reason=parent_venv_corrupted "
            f"editable_path={resolved_path} parent_venv={parent_venv} "
            "parent venv is corrupted: editable install points outside repo_root; "
            "repair the parent venv, then recreate the affected worktrees"
        ) from exc


def _is_file_requirement(requirement: str) -> bool:
    if _requirement_file_path(requirement) is not None:
        return True
    _name, separator, url = requirement.partition(" @ ")
    return bool(separator) and _requirement_file_path(url) is not None


def _editable_requirement_spec(requirement: str) -> str | None:
    for prefix in ("-e ", "--editable "):
        if requirement.startswith(prefix):
            return requirement.removeprefix(prefix).strip()
    return None


def _requirement_file_path(spec: str) -> Path | None:
    parsed = urlsplit(spec)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    if parsed.scheme:
        return None
    path = Path(spec)
    if path.is_absolute():
        return path
    return None


def _find_uv() -> Path | None:
    uv = shutil.which("uv")
    return Path(uv) if uv else None


def _run_venv_command(
    cmd: list[str], *, cwd: Path, reason: str
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result
    output = (result.stderr or result.stdout).strip()
    if len(output) > 2000:
        output = output[-2000:]
    raise WorktreeVenvError(
        "worktree_venv_creation_failed: "
        f"reason={reason} returncode={result.returncode} "
        f"command={shlex.join(cmd)} output={output}"
    )


def _batched(items: tuple[str, ...], size: int) -> list[tuple[str, ...]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _worktree_paths_for_branch(repo_root: Path, branch: str) -> list[Path]:
    repo_toplevel = _repo_toplevel(repo_root)
    result = run_git(
        ["worktree", "list", "--porcelain"],
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        return []

    paths: list[Path] = []
    current_path: Path | None = None
    branch_ref = f"refs/heads/{branch}"
    for line in result.stdout.splitlines():
        if not line:
            current_path = None
            continue
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ")).resolve()
            continue
        if line.startswith("branch ") and current_path is not None:
            if line.removeprefix("branch ") == branch_ref:
                if current_path != repo_toplevel:
                    paths.append(current_path)
    return paths


def _repo_toplevel(repo_root: Path) -> Path:
    result = run_git(
        ["rev-parse", "--show-toplevel"],
        cwd=repo_root,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return repo_root.resolve()


def _auto_remove_worktree(
    repo_root: Path,
    path: Path,
    *,
    branch: str,
    base_ref: str,
) -> None:
    _raise_if_dirty_worktree(path)
    _rescue_detached_head_before_remove(
        repo_root=repo_root,
        path=path,
        branch=branch,
        base_ref=base_ref,
    )
    _log_worktree_autoclean(path, level=logging.INFO)
    result = run_git(
        ["worktree", "remove", "--force", str(path)],
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        LOGGER.warning(
            "event=worktree_autoclean path=%s reason=%s remove_failed=true returncode=%s stderr=%s",
            path,
            _AUTO_CLEAN_REASON,
            result.returncode,
            result.stderr.strip(),
            extra={
                "event": "worktree_autoclean",
                "path": str(path),
                "reason": _AUTO_CLEAN_REASON,
                "remove_failed": True,
                "returncode": result.returncode,
            },
        )


def _raise_if_dirty_worktree(path: Path) -> None:
    if not path.exists():
        return
    try:
        result = run_git(
            ["status", "--porcelain"],
            cwd=path,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning(
            "event=worktree_autoclean path=%s reason=%s status_failed=true error=%s",
            path,
            _AUTO_CLEAN_REASON,
            exc,
            extra={
                "event": "worktree_autoclean",
                "path": str(path),
                "reason": _AUTO_CLEAN_REASON,
                "status_failed": True,
            },
        )
        raise ValueError(
            f"refusing to auto-clean worktree without confirmed clean status: {path}"
        ) from exc
    if result.returncode != 0:
        LOGGER.warning(
            "event=worktree_autoclean path=%s reason=%s status_failed=true "
            "returncode=%s stderr=%s",
            path,
            _AUTO_CLEAN_REASON,
            result.returncode,
            str(result.stderr or "").strip(),
            extra={
                "event": "worktree_autoclean",
                "path": str(path),
                "reason": _AUTO_CLEAN_REASON,
                "status_failed": True,
                "returncode": result.returncode,
            },
        )
        raise ValueError(
            f"refusing to auto-clean worktree without confirmed clean status: {path}"
        )
    try:
        stdout = _require_git_text_output(result, operation="inspect worktree status")
    except ValueError as exc:
        raise ValueError(
            f"refusing to auto-clean worktree without confirmed clean status: {path}"
        ) from exc
    if stdout != "":
        raise ValueError(f"refusing to auto-clean dirty worktree: {path}")


def _rescue_detached_head_before_remove(
    *,
    repo_root: Path,
    path: Path,
    branch: str,
    base_ref: str,
) -> None:
    if not path.exists():
        return
    head_state = run_git(
        ["symbolic-ref", "--quiet", "HEAD"],
        cwd=path,
        check=False,
    )
    if head_state.returncode == 0:
        stdout = _require_git_text_output(head_state, operation="inspect HEAD state")
        if not stdout.strip().startswith("refs/"):
            raise ValueError(
                f"refusing to auto-clean worktree with uncertain HEAD state: {path}"
            )
        return
    if head_state.returncode != 1:
        raise ValueError(
            f"refusing to auto-clean worktree with uncertain HEAD state: {path}"
        )

    missing_result = run_git(
        ["rev-list", "--max-count=20", "HEAD", "--not", base_ref, "--remotes"],
        cwd=path,
        check=True,
    )
    commits = _parse_git_commit_lines(
        missing_result,
        operation="inspect detached HEAD commits missing from base and remotes",
    )
    if not commits:
        return

    rescue = _create_branch_rescue_ref(
        repo_root=repo_root,
        branch=branch,
        source_ref="HEAD",
        source_cwd=path,
        event="detached_head_rescue_created",
    )
    rescue = _persist_branch_rescue_artifact(
        repo_root=repo_root,
        branch=branch,
        rescue=rescue,
    )
    LOGGER.warning(
        "event=detached_head_rescued_before_autoclean branch=%s path=%s "
        "base_ref=%s rescue_ref=%s rescued_sha=%s commits=%s",
        branch,
        path,
        base_ref,
        rescue.ref,
        rescue.sha,
        ",".join(commits),
        extra={
            "event": "detached_head_rescued_before_autoclean",
            "branch": branch,
            "path": str(path),
            "base_ref": base_ref,
            "rescue_ref": rescue.ref,
            "rescued_sha": rescue.sha,
            "commits": commits,
        },
    )


def _require_git_text_output(
    result: subprocess.CompletedProcess[str], *, operation: str
) -> str:
    if result.returncode != 0:
        result.check_returncode()
    if not isinstance(result.stdout, str):
        raise ValueError(f"git returned unparseable output while attempting to {operation}")
    return result.stdout


def _parse_git_commit_lines(
    result: subprocess.CompletedProcess[str], *, operation: str
) -> list[str]:
    stdout = _require_git_text_output(result, operation=operation)
    commits = stdout.splitlines()
    if any(
        len(commit) not in {40, 64}
        or any(character not in "0123456789abcdefABCDEF" for character in commit)
        for commit in commits
    ):
        raise ValueError(f"git returned unparseable output while attempting to {operation}")
    return commits


def _log_worktree_autoclean(path: Path, *, level: int) -> None:
    LOGGER.log(
        level,
        "event=worktree_autoclean path=%s reason=%s",
        path,
        _AUTO_CLEAN_REASON,
        extra={
            "event": "worktree_autoclean",
            "path": str(path),
            "reason": _AUTO_CLEAN_REASON,
        },
    )


def _ref_exists(repo_root: Path, ref: str) -> bool:
    result = run_git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo_root,
        check=False,
    )
    return result.returncode == 0


def _ensure_ref_available(repo_root: Path, ref: str) -> None:
    """Fetch remote refs like origin/branch when they are not present locally."""
    if _ref_exists(repo_root, ref):
        return
    remote, _, branch = ref.partition("/")
    if not remote or not branch or ref.startswith("refs/"):
        return
    run_git(
        ["fetch", remote, f"{branch}:refs/remotes/{remote}/{branch}"],
        cwd=repo_root,
        check=False,
    )
