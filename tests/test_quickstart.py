"""End-to-end: the README quickstart, run through the real pipeline.

No mocks. The demo repository is built by the shipped setup script, the
"agent" is the bundled `fake` CLI (atlas_dispatch/fake_agent.py) resolved
through the model registry, and every stage (worktree, CLI subprocess,
auto-commit, allowlist, protected paths, acceptance, ref guard, classifier,
report) runs for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from atlas_dispatch import dispatch

REPO_ROOT = Path(__file__).resolve().parents[1]
QUICKSTART = REPO_ROOT / "examples" / "quickstart"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="the quickstart needs bash and python3 on PATH",
)


@pytest.fixture()
def demo_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(
        ["bash", str(QUICKSTART / "setup_demo_repo.sh"), str(tmp_path / "demo")],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.delenv("ATLAS_DISPATCH_FAKE_CMD", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_PROTECTED_PATTERNS", raising=False)
    return tmp_path / "demo" / "calc"


def _only_run(repo: Path, task_id: str) -> tuple[dict[str, object], str]:
    run_dirs = list((repo / ".atlas-dispatch" / "runs" / task_id).iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    return summary, report


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_well_behaved_run_verifies_and_leaves_main_alone(
    demo_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main_before = _git(demo_repo, "rev-parse", "main")

    rc = dispatch(demo_repo / "dispatch_tasks" / "T-001-add-subtract.json")

    stdout = capsys.readouterr().out
    assert "not in the registry" not in stdout
    assert "publish_ok=not_requested" in stdout

    summary, report = _only_run(demo_repo, "T-001")
    assert rc == 0
    assert summary["classification"]["kind"] == "success"
    assert summary["verify_passed"] is True
    assert summary["changed_files"] == ["calc.py", "test_calc.py"]
    assert summary["orchestrator_ref_guard"]["passed"] is True
    assert "- Verification: **VERIFIED_PASS**" in report
    assert "## Branch publication\n\n- Not requested." in report
    # The harness never merges: main is untouched, the work is on the branch.
    assert _git(demo_repo, "rev-parse", "main") == main_before
    assert _git(demo_repo, "log", "-1", "--format=%s", "fake/t-001-subtract") == (
        "fake(T-001): Add a subtract function"
    )


def test_agent_that_moves_main_is_caught_by_the_ref_guard(demo_repo: Path) -> None:
    rc = dispatch(demo_repo / "dispatch_tasks" / "T-002-agent-moves-main.json")

    summary, report = _only_run(demo_repo, "T-002")
    assert rc == 1
    # The CLI itself "succeeded"; exit 0 is not the verdict.
    assert summary["classification"]["kind"] == "success"
    assert summary["verify_passed"] is False
    findings = summary["orchestrator_ref_guard"]["findings"]
    assert any(finding.startswith("main moved during this run") for finding in findings)
    assert "A REF OUTSIDE THE WORKTREE MOVED" in report


def test_rate_limited_run_is_classified_with_a_next_action(demo_repo: Path) -> None:
    rc = dispatch(demo_repo / "dispatch_tasks" / "T-003-rate-limited.json")

    summary, report = _only_run(demo_repo, "T-003")
    assert rc == 1
    assert summary["classification"]["kind"] == "rate_limited"
    assert "Wait and retry" in summary["classification"]["suggested_action"]
    assert summary["acceptance_skip_reason"] == ["cli_failed", "no_files_changed"]
    assert "- Kind: **rate_limited**" in report


def test_protected_path_fails_verification_even_when_acceptance_passes(
    demo_repo: Path,
) -> None:
    rc = dispatch(demo_repo / "dispatch_tasks" / "T-004-touches-protected-path.json")

    summary, report = _only_run(demo_repo, "T-004")
    assert rc == 1
    assert "- Verification: **VERIFIED_FAIL**" in report
    assert summary["acceptance_outcome"] == "passed"
    assert summary["protected_path_violations"] == ["migrations/0001_add_index.sql"]
    assert summary["verify_passed"] is False
