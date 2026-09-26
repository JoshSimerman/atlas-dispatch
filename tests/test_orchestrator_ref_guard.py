"""The dispatched agent must not be able to move refs outside its worktree.

A worktree is a filesystem boundary, not a git-ref boundary -- every worktree
shares one `.git` -- so `allowed_paths` cannot see a moved ref. Background:
ADR-003 in docs/DESIGN_DECISIONS.md.

These tests assert the DETECTION fires. A guard only ever shown passing on a
cooperative run has not been shown able to go red.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_dispatch.adapter import AdapterResult
from atlas_dispatch.dispatcher import (
    ORCHESTRATOR_OBSERVED_REMOTE_REFS,
    ORCHESTRATOR_PROTECTED_REFS,
    _capture_orchestrator_refs,
    _diff_orchestrator_refs,
    _orchestrator_ref_report_lines,
    dispatch,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-b", "main", str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "T")
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-m", "seed")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work, origin


def test_unchanged_refs_produce_no_findings(tmp_path: Path) -> None:
    work, _ = _repo_with_origin(tmp_path)
    before = _capture_orchestrator_refs(work)
    after = _capture_orchestrator_refs(work)
    assert _diff_orchestrator_refs(before, after) == []
    assert before["refs"]["main"] is not None
    assert before["refs"]["HEAD"] == before["refs"]["main"]
    assert before["refs"]["origin/main"] == before["refs"]["main"]
    report = "\n".join(
        _orchestrator_ref_report_lines(
            {
                "before": before,
                "after": after,
                "findings": [],
                "passed": True,
            }
        )
    )
    for ref in (*ORCHESTRATOR_PROTECTED_REFS, *ORCHESTRATOR_OBSERVED_REMOTE_REFS):
        assert f"- {ref} before: `{before['refs'][ref]}`" in report
        assert f"- {ref} after: `{after['refs'][ref]}`" in report


def test_local_main_moved_is_detected(tmp_path: Path) -> None:
    """The common case: agent fast-forwards local main, never pushes."""
    work, _ = _repo_with_origin(tmp_path)
    before = _capture_orchestrator_refs(work)
    (work / "agent.txt").write_text("agent\n", encoding="utf-8")
    _git(work, "add", "agent.txt")
    _git(work, "commit", "-m", "an agent moved local main")
    findings = _diff_orchestrator_refs(before, _capture_orchestrator_refs(work))
    assert any(f.startswith("main moved during this run:") for f in findings), findings
    assert any(f.startswith("HEAD moved during this run:") for f in findings), findings


def test_protected_refs_names_main() -> None:
    assert ORCHESTRATOR_PROTECTED_REFS == ("main", "HEAD")
    assert ORCHESTRATOR_OBSERVED_REMOTE_REFS == ("origin/main",)


def test_direct_push_to_origin_main_is_detected(tmp_path: Path) -> None:
    """A refspec can move origin/main without moving either local ref."""
    work, _ = _repo_with_origin(tmp_path)
    before = _capture_orchestrator_refs(work)
    agent_worktree = tmp_path / "agent-worktree"
    _git(work, "worktree", "add", "-b", "agent", str(agent_worktree), "main")
    (agent_worktree / "agent.txt").write_text("agent\n", encoding="utf-8")
    _git(agent_worktree, "add", "agent.txt")
    _git(agent_worktree, "commit", "-m", "unreviewed agent commit")
    _git(agent_worktree, "push", "-q", "origin", "HEAD:refs/heads/main")

    findings = _diff_orchestrator_refs(before, _capture_orchestrator_refs(work))

    assert any(
        finding.startswith("origin/main moved during this run:")
        for finding in findings
    ), findings
    assert not any(
        finding.startswith("main moved during this run:") for finding in findings
    ), findings
    assert not any(
        finding.startswith("HEAD moved during this run:") for finding in findings
    ), findings


def test_a_repository_without_main_is_not_an_unreadable_ref(tmp_path: Path) -> None:
    """A base branch called `master` is a readable fact, not a read failure."""
    work = tmp_path / "master-only"
    subprocess.run(
        ["git", "init", "-b", "master", str(work)],
        check=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "T")
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-m", "seed")

    before = _capture_orchestrator_refs(work)
    assert before["refs"]["main"] is None
    assert "main" not in before["errors"]
    assert before["refs"]["HEAD"]
    assert _diff_orchestrator_refs(before, before) == []

    # HEAD is still guarded in such a repository.
    (work / "moved.txt").write_text("moved\n", encoding="utf-8")
    _git(work, "add", "moved.txt")
    _git(work, "commit", "-m", "moved")
    after = _capture_orchestrator_refs(work)
    assert any(
        finding.startswith("HEAD moved during this run:")
        for finding in _diff_orchestrator_refs(before, after)
    )


def test_main_appearing_during_the_run_is_a_finding(tmp_path: Path) -> None:
    work = tmp_path / "master-only"
    subprocess.run(
        ["git", "init", "-b", "master", str(work)],
        check=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "T")
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-m", "seed")

    before = _capture_orchestrator_refs(work)
    _git(work, "branch", "main")
    after = _capture_orchestrator_refs(work)

    assert any(
        finding.startswith("main moved during this run: unresolved ->")
        for finding in _diff_orchestrator_refs(before, after)
    )


def test_unreadable_local_protected_ref_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import atlas_dispatch.dispatcher as dispatcher_mod

    real_run_git = dispatcher_mod._run_git

    def corrupt_main(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and any("refs/heads/main" in a for a in args):
            return subprocess.CompletedProcess(
                ["git", *args], 128, "", "fatal: bad object refs/heads/main\n"
            )
        return real_run_git(repo, args)

    work = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "T")
    (work / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-m", "seed")
    monkeypatch.setattr(dispatcher_mod, "_run_git", corrupt_main)

    snapshot = _capture_orchestrator_refs(work)
    findings = _diff_orchestrator_refs(snapshot, snapshot)
    report = "\n".join(
        _orchestrator_ref_report_lines(
            {
                "before": snapshot,
                "after": snapshot,
                "findings": findings,
                "passed": not findings,
            }
        )
    )

    assert any("could not record main before dispatch" in item for item in findings)
    assert "- Ref guard passed: **False**" in report
    assert "main before error:" in report


def test_dispatch_fails_and_reports_before_after_when_agent_moves_main(
    tmp_path: Path,
) -> None:
    """A successful build is still failed when its agent moves local main."""
    work, _ = _repo_with_origin(tmp_path)
    before_sha = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Move main, then finish the assigned file.\n", encoding="utf-8")
    runs = tmp_path / "runs"
    spec = tmp_path / "task.json"
    spec.write_text(
        json.dumps(
            {
                "id": "REF-GUARD-REGRESSION",
                "title": "Detect an agent moving main",
                "target_repo": str(work),
                "cli": "codex",
                "prompt_template": str(prompt),
                "worktree_branch": "codex/ref-guard-regression",
                "allowed_paths": ["result.txt"],
                "acceptance": ["test -f result.txt"],
                "runs_dir": str(runs),
                "push_branch": False,
            }
        ),
        encoding="utf-8",
    )
    moved_sha: list[str] = []

    def agent_moves_main(**kwargs: object) -> AdapterResult:
        cwd = Path(str(kwargs["cwd"]))
        (cwd / "absorbed-by-main.txt").write_text("agent commit\n", encoding="utf-8")
        _git(cwd, "add", "absorbed-by-main.txt")
        _git(cwd, "commit", "-m", "agent commit used to move main")
        moved_sha.append(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        _git(cwd, "update-ref", "refs/heads/main", moved_sha[0])
        # Leave a second, allowed change for the harness to auto-commit. This
        # keeps acceptance green so the ref guard is the only failing gate.
        (cwd / "result.txt").write_text("finished\n", encoding="utf-8")
        return AdapterResult(
            cli="codex",
            exit_code=0,
            stdout="done\n",
            stderr="",
            duration_seconds=0.01,
            command=["agent-under-test"],
        )

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=agent_moves_main):
        exit_code = dispatch(spec, no_reuse=True)

    run_dir = next(runs.iterdir())
    summary = json.loads(
        (run_dir / "cli.summary.json").read_text(encoding="utf-8")
    )
    report = (run_dir / "report.md").read_text(encoding="utf-8")

    assert moved_sha and moved_sha[0] != before_sha
    assert exit_code == 1
    assert summary["verify_passed"] is False
    assert summary["acceptance_outcome"] == "passed"
    guard = summary["orchestrator_ref_guard"]
    assert guard["passed"] is False
    assert guard["before"]["refs"]["main"] == before_sha
    assert guard["before"]["refs"]["HEAD"] == before_sha
    assert guard["after"]["refs"]["main"] == moved_sha[0]
    assert guard["after"]["refs"]["HEAD"] == moved_sha[0]
    assert f"main moved during this run: {before_sha} -> {moved_sha[0]}" in report
    assert f"HEAD moved during this run: {before_sha} -> {moved_sha[0]}" in report
    assert "- Verify passed: **False**" in report
    # Detection is the control in this pass. The harness does not silently
    # restore refs before a human has seen what happened.
    current_main = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_main == moved_sha[0]
