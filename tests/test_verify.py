"""Verification gate tests."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from atlas_dispatch import run_acceptance_commands, verify
from atlas_dispatch.verify import (
    PROTECTED_PATTERNS,
    CheckResult,
    VerificationState,
    VerifyReport,
    check_protected_paths,
    parse_verification_state,
    protected_patterns,
)


def _python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int, *, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.05)
    return not _pid_exists(pid)


def test_acceptance_command_completes_within_timeout(tmp_path: Path) -> None:
    results = run_acceptance_commands(
        commands=[_python_command("print('ok')")],
        cwd=tmp_path,
        timeout_seconds=2,
    )

    assert len(results) == 1
    assert results[0].passed
    assert results[0].classification == "success"
    assert results[0].details == "ok"
    assert results.exit_codes == [0]


def test_non_import_failure_retains_exact_exit_code_without_provenance_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_provenance_probe(*args: object, **kwargs: object) -> None:
        raise AssertionError("non-import command must not be provenance-probed")

    monkeypatch.setattr(
        verify.import_provenance,
        "run_import_provenance_check",
        unexpected_provenance_probe,
    )

    results = run_acceptance_commands(
        commands=[_python_command("raise SystemExit(23)")],
        cwd=tmp_path,
        timeout_seconds=2,
        import_provenance_required=True,
    )

    assert len(results) == 1
    assert not results[0].passed
    assert results[0].classification == "acceptance_failed"
    assert results.exit_codes == [23]


def test_verify_report_acceptance_outcome_is_tristate() -> None:
    skipped = VerifyReport(
        changed_files_present=True,
        allowlist_passed=False,
        out_of_scope_paths=["not-allowed.txt"],
        acceptance_skip_reason=["allowlist_failed"],
    )
    failed = VerifyReport(
        changed_files_present=True,
        allowlist_passed=True,
        command_results=[
            CheckResult(
                name="python -c fail",
                passed=False,
                classification="acceptance_failed",
            )
        ],
    )
    passed = VerifyReport(
        changed_files_present=True,
        allowlist_passed=True,
        command_results=[CheckResult(name="python -c pass", passed=True)],
    )

    assert skipped.acceptance_outcome == "skipped"
    assert skipped.passed is False
    assert failed.acceptance_outcome == "failed"
    assert failed.passed is False
    assert passed.acceptance_outcome == "passed"
    assert passed.passed is True
    assert failed.verification_state is VerificationState.VERIFIED_FAIL
    assert passed.verification_state is VerificationState.VERIFIED_PASS


def test_verify_report_provenance_refusal_is_not_attempted() -> None:
    for classification in ("unknown", "stale_import_binding"):
        details = f"import provenance guard refused acceptance ({classification})"
        report = VerifyReport(
            changed_files_present=True,
            allowlist_passed=True,
            command_results=[
                CheckResult(
                    name="import provenance guard",
                    passed=False,
                    details=details,
                    classification=classification,
                    pytest_counts="unavailable",
                )
            ],
        )

        assert report.acceptance_outcome == "not_attempted"
        assert report.acceptance_not_attempted_reason
        assert details in report.acceptance_not_attempted_reason
        assert report.verification_state is None
        assert report.passed is False


def test_verify_report_mixed_non_refusal_results_still_fail() -> None:
    report = VerifyReport(
        changed_files_present=True,
        allowlist_passed=True,
        command_results=[
            CheckResult(name="python -c pass", passed=True),
            CheckResult(
                name="python -c fail",
                passed=False,
                classification="acceptance_failed",
            ),
        ],
    )

    assert report.acceptance_outcome == "failed"
    assert report.verification_state is VerificationState.VERIFIED_FAIL
    assert report.acceptance_not_attempted_reason == ""


def test_verify_report_scope_prediction_is_informational_but_protected_paths_gate() -> None:
    passing_check = CheckResult(name="python -c pass", passed=True)
    prediction_miss = VerifyReport(
        changed_files_present=True,
        allowlist_passed=False,
        forbidden_violations=["services/new-service/app.py"],
        out_of_scope_paths=["services/new-service/app.py"],
        command_results=[passing_check],
    )
    protected_crossing = VerifyReport(
        changed_files_present=True,
        allowlist_passed=True,
        protected_path_violations=["migrations/0042_drop_users.sql"],
        command_results=[passing_check],
    )

    assert prediction_miss.passed is True
    assert protected_crossing.passed is False
    assert protected_crossing.verification_state is VerificationState.VERIFIED_FAIL


def test_default_protected_patterns_are_generic_examples() -> None:
    assert PROTECTED_PATTERNS == (
        "configs/prod.*",
        "migrations/**",
        "secrets/**",
        ".github/workflows/**",
    )
    assert protected_patterns({}) == PROTECTED_PATTERNS


def test_protected_patterns_env_replaces_defaults() -> None:
    env = {"ATLAS_DISPATCH_PROTECTED_PATTERNS": "infra/**, deploy/*.yaml\nbilling/**"}

    assert protected_patterns(env) == ("infra/**", "deploy/*.yaml", "billing/**")
    assert protected_patterns({"ATLAS_DISPATCH_PROTECTED_PATTERNS": ""}) == ()


def test_check_protected_paths_matches_changed_files_and_ignores_harness_files() -> None:
    changed = [
        "src/app.py",
        "migrations/0001_init.sql",
        "configs/prod.yaml",
        "configs/dev.yaml",
        ".atlas-dispatch-mcp.json",
    ]

    assert check_protected_paths(
        changed_files=changed, patterns=PROTECTED_PATTERNS
    ) == ["migrations/0001_init.sql", "configs/prod.yaml"]
    assert check_protected_paths(
        changed_files=[".atlas-dispatch-mcp.json"], patterns=("**",)
    ) == []


def test_check_protected_paths_reads_the_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_DISPATCH_PROTECTED_PATTERNS", "billing/**")

    assert check_protected_paths(
        changed_files=["billing/charge.py", "migrations/0001.sql"]
    ) == ["billing/charge.py"]


def test_verification_state_discriminates_no_change_and_rejects_unknown() -> None:
    no_change = VerifyReport(
        changed_files_present=False,
        allowlist_passed=True,
        acceptance_skip_reason=["no_files_changed"],
    )
    other_skip = VerifyReport(
        changed_files_present=True,
        allowlist_passed=True,
        acceptance_skip_reason=["allowlist_failed"],
    )

    assert no_change.verification_state is VerificationState.NOT_RUN_NO_CHANGE
    assert other_skip.verification_state is None
    assert parse_verification_state("NOT_RUN_SPEC_DEFECT") is (
        VerificationState.NOT_RUN_SPEC_DEFECT
    )
    assert parse_verification_state("verified_pass") is None
    assert parse_verification_state(None) is None


def test_acceptance_command_exceeds_timeout_kills_subprocess(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "pid.txt"
    command = _python_command(
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )

    results = run_acceptance_commands(
        commands=[command],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    pid = int(pid_path.read_text(encoding="utf-8"))
    assert len(results) == 1
    assert not results[0].passed
    assert results[0].classification == "acceptance_timeout"
    assert "timed out after 1s" in results[0].details
    assert results.exit_codes == [None]
    assert _wait_for_pid_exit(pid)


def test_acceptance_timeout_kills_child_tree(tmp_path: Path) -> None:
    pid_path = tmp_path / "pids.txt"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"Path({str(pid_path)!r}).write_text("
        "f'{os.getpid()}\\n{child.pid}\\n', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )

    results = run_acceptance_commands(
        commands=[_python_command(parent_code)],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    parent_pid, child_pid = [
        int(line) for line in pid_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(results) == 1
    assert results[0].classification == "acceptance_timeout"
    assert _wait_for_pid_exit(parent_pid)
    assert _wait_for_pid_exit(child_pid)


def test_acceptance_5min_warning_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakePopen:
        pid = 12345
        returncode = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "ok", ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    ticks = iter([0.0, 301.0])
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))

    with caplog.at_level("WARNING", logger=verify.LOGGER.name):
        results = run_acceptance_commands(
            commands=["slow command"],
            cwd=tmp_path,
            timeout_seconds=10,
        )

    assert results[0].passed
    warning = next(
        record
        for record in caplog.records
        if record.message == "acceptance command exceeded slow threshold"
    )
    assert warning.__dict__["acceptance_command"] == "slow command"
    assert warning.__dict__["elapsed_seconds"] == 301.0
    assert warning.__dict__["threshold_seconds"] == 300.0


def test_acceptance_continues_after_one_timeout(tmp_path: Path) -> None:
    marker = tmp_path / "third-ran.txt"
    timeout_command = _python_command("import time; time.sleep(60)")
    third_command = _python_command(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('done', encoding='utf-8')"
    )

    results = run_acceptance_commands(
        commands=[
            _python_command("print('first')"),
            timeout_command,
            third_command,
        ],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert [result.classification for result in results] == [
        "success",
        "acceptance_timeout",
        "success",
    ]
    assert results.exit_codes == [0, None, 0]
    assert marker.read_text(encoding="utf-8") == "done"


