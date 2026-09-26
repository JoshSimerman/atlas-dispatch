"""`no_change` must mean the run produced nothing, not that HEAD is an ancestor.

An agent that builds, commits, merges into main and pushes leaves
`base_ref..HEAD` legitimately at 0. Classifying that run `no_change` suggests a
redispatch, which force-resets the branch and destroys the merged work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atlas_dispatch.dispatcher import _should_classify_no_change, _worktree_head_sha


class _Result:
    def __init__(self, exit_code=0, timed_out=False):
        self.exit_code = exit_code
        self.timed_out = timed_out


class _WT:
    def __init__(self, path: Path):
        self.worktree_path = path


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    p = tmp_path / "r"
    p.mkdir()
    _git(p, "init", "-q", "-b", "main")
    _git(p, "config", "user.email", "t@example.com")
    _git(p, "config", "user.name", "t")
    (p / "f.txt").write_text("one\n")
    _git(p, "add", "-A")
    _git(p, "commit", "-qm", "base")
    return p


def test_a_run_that_changed_nothing_is_no_change(repo):
    head = _worktree_head_sha(repo)
    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(), base_ref="main", head_before_cli=head
    ) is True


def test_a_run_that_committed_is_not_no_change(repo):
    head = _worktree_head_sha(repo)
    (repo / "f.txt").write_text("two\n")
    _git(repo, "commit", "-aqm", "work")
    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(), base_ref="main", head_before_cli=head
    ) is False


def test_a_run_whose_work_was_merged_into_base_ref_is_not_no_change(repo):
    """THE REGRESSION. The agent commits on its branch and merges to main, so
    HEAD is an ancestor of base_ref and `main..HEAD` is 0 -- which the old
    predicate read as 'produced no commits'."""
    _git(repo, "checkout", "-qb", "agent/work")
    head = _worktree_head_sha(repo)
    (repo / "f.txt").write_text("two\n")
    _git(repo, "commit", "-aqm", "the agent's work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "agent/work", "-m", "merge")
    _git(repo, "checkout", "-q", "agent/work")

    assert int(_git(repo, "rev-list", "--count", "main..HEAD")) == 0, "precondition"
    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(), base_ref="main", head_before_cli=head
    ) is False


def test_base_ref_moving_ahead_under_a_long_run_is_not_no_change(repo):
    """The same collapse from the other direction: nobody merged the agent's
    work, base_ref simply advanced past it while the run was in flight."""
    _git(repo, "checkout", "-qb", "agent/work")
    head = _worktree_head_sha(repo)
    (repo / "f.txt").write_text("two\n")
    _git(repo, "commit", "-aqm", "the agent's work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "agent/work")
    (repo / "g.txt").write_text("someone else\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated push")
    _git(repo, "checkout", "-q", "agent/work")

    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(), base_ref="main", head_before_cli=head
    ) is False


def test_uncommitted_work_is_never_no_change(repo):
    head = _worktree_head_sha(repo)
    (repo / "f.txt").write_text("dirty\n")
    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(), base_ref="main", head_before_cli=head
    ) is False


def test_a_failed_or_timed_out_run_is_never_no_change(repo):
    head = _worktree_head_sha(repo)
    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(exit_code=1), base_ref="main", head_before_cli=head
    ) is False
    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(timed_out=True), base_ref="main", head_before_cli=head
    ) is False


def test_without_a_recorded_head_it_falls_back_to_the_old_predicate(repo):
    """Callers that cannot supply head_before_cli keep the previous behaviour
    rather than silently changing meaning."""
    assert _should_classify_no_change(
        worktree=_WT(repo), cli_result=_Result(), base_ref="main"
    ) is True
