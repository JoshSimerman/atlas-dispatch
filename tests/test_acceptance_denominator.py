"""Acceptance denominator capture and collection-floor controls."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from atlas_dispatch.dispatcher import (
    _acceptance_command_summary,
    load_task,
)
from atlas_dispatch.verify import (
    PYTEST_COUNTS_UNAVAILABLE,
    _parse_pytest_counts,
    run_acceptance_commands,
)


def _python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _pytest_command(path: Path, *args: str) -> str:
    tokens = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(path), *args]
    return shlex.join(tokens)


def _write_passing_tests(tmp_path: Path) -> Path:
    path = tmp_path / "test_sample.py"
    path.write_text(
        "def test_keep():\n"
        "    assert True\n\n"
        "def test_drop():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    return path


# Captured verbatim from:
#   python -m pytest -p no:cacheprovider tests/test_verify.py
#       -k test_acceptance_command_completes_within_timeout
# This is intentionally real pytest output rather than text designed around the
# parser regex. The rootdir line is omitted because it carries only a host path.
_ACTUAL_PYTEST_DESELECTION_OUTPUT = """\
============================= test session starts ==============================
platform darwin -- Python 3.14.4, pytest-9.0.3, pluggy-1.6.0
configfile: pyproject.toml
plugins: anyio-4.13.0
collected 34 items / 33 deselected / 1 selected

tests/test_verify.py .                                                   [100%]

======================= 1 passed, 33 deselected in 0.43s =======================
"""

# Real `pytest --collect-only -q` collection-error summary output, preserved
# verbatim from a failed run.
_ACTUAL_PYTEST_COLLECTION_ERROR_OUTPUT = """\
!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!
4088 tests collected, 2 errors in 3.00s
"""


def test_parser_reads_actual_pytest_deselection_output() -> None:
    assert _parse_pytest_counts(_ACTUAL_PYTEST_DESELECTION_OUTPUT, "") == {
        "collected": 34,
        "deselected": 33,
        "errors": 0,
        "passed": 1,
        "failed": 0,
        "xfailed": 0,
        "skipped": 0,
    }


def test_parser_reads_actual_pytest_collection_error_output() -> None:
    assert _parse_pytest_counts(_ACTUAL_PYTEST_COLLECTION_ERROR_OUTPUT, "") == {
        "collected": 4088,
        "deselected": 0,
        "errors": 2,
        "passed": 0,
        "failed": 0,
        "xfailed": 0,
        "skipped": 0,
    }


def test_empty_pytest_suite_is_not_counts_unavailable() -> None:
    output = "collected 0 items\n\n================ no tests ran in 0.01s ================"

    counts = _parse_pytest_counts(output, "")

    assert counts != PYTEST_COUNTS_UNAVAILABLE
    assert counts["collected"] == 0


def test_real_pytest_run_captures_deselected_denominator(tmp_path: Path) -> None:
    test_path = _write_passing_tests(tmp_path)

    results = run_acceptance_commands(
        commands=[_pytest_command(test_path, "-k", "keep")],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert results[0].passed is True
    assert results[0].pytest_counts == {
        "collected": 2,
        "deselected": 1,
        "errors": 0,
        "passed": 1,
        "failed": 0,
        "xfailed": 0,
        "skipped": 0,
    }


def test_real_pytest_run_captures_collection_errors(tmp_path: Path) -> None:
    test_path = tmp_path / "test_collection_error.py"
    test_path.write_text(
        "raise RuntimeError('actual collection failure')\n",
        encoding="utf-8",
    )

    results = run_acceptance_commands(
        commands=[_pytest_command(test_path)],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert results[0].classification == "acceptance_failed"
    assert results[0].pytest_counts == {
        "collected": 0,
        "deselected": 0,
        "errors": 1,
        "passed": 0,
        "failed": 0,
        "xfailed": 0,
        "skipped": 0,
    }
    assert "ERROR collecting" in results[0].details


def test_real_pytest_run_captures_every_requested_outcome(tmp_path: Path) -> None:
    test_path = tmp_path / "test_mixed_outcomes.py"
    test_path.write_text(
        "import pytest\n\n"
        "def test_passes():\n"
        "    assert True\n\n"
        "def test_fails():\n"
        "    assert False\n\n"
        "@pytest.mark.skip(reason='captured skip')\n"
        "def test_skips():\n"
        "    pass\n\n"
        "@pytest.mark.xfail(reason='captured xfail')\n"
        "def test_xfails():\n"
        "    assert False\n",
        encoding="utf-8",
    )

    results = run_acceptance_commands(
        commands=[_pytest_command(test_path)],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert results[0].pytest_counts == {
        "collected": 4,
        "deselected": 0,
        "errors": 0,
        "passed": 1,
        "failed": 1,
        "xfailed": 1,
        "skipped": 1,
    }


def test_declared_floor_below_collection_still_passes(tmp_path: Path) -> None:
    test_path = _write_passing_tests(tmp_path)
    command = _pytest_command(test_path)

    results = run_acceptance_commands(
        commands=[command],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected=1,
    )

    assert results[0].passed is True
    assert results[0].classification == "success"
    assert results[0].pytest_counts["collected"] == 2


def test_floor_wins_over_success_when_collection_shrinks(tmp_path: Path) -> None:
    test_path = _write_passing_tests(tmp_path)

    results = run_acceptance_commands(
        commands=[_pytest_command(test_path)],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected=3,
    )

    assert results[0].passed is False
    assert results[0].classification == "acceptance_collection_shrank"
    assert "collected 2 < 3" in results[0].details


def test_exit_zero_unparseable_output_cannot_satisfy_floor(tmp_path: Path) -> None:
    results = run_acceptance_commands(
        commands=[_python_command("print('ordinary successful output')")],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected=1,
    )

    assert results[0].passed is False
    assert results[0].classification == "acceptance_collection_shrank"
    assert results[0].pytest_counts == PYTEST_COUNTS_UNAVAILABLE
    assert "pytest counts are unavailable" in results[0].details


def test_no_floor_preserves_exit_zero_unparseable_behavior(tmp_path: Path) -> None:
    results = run_acceptance_commands(
        commands=[_python_command("print('ordinary successful output')")],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert results[0].passed is True
    assert results[0].classification == "success"
    assert results[0].details == "ordinary successful output"
    assert results[0].pytest_counts == PYTEST_COUNTS_UNAVAILABLE
    assert results.exit_codes == [0]
    assert _acceptance_command_summary(results)[0]["pytest_counts"] == "unavailable"


def test_genuine_acceptance_failure_remains_acceptance_failure(tmp_path: Path) -> None:
    test_path = tmp_path / "test_failure.py"
    test_path.write_text(
        "def test_passes():\n"
        "    assert True\n\n"
        "def test_fails():\n"
        "    assert False\n",
        encoding="utf-8",
    )

    results = run_acceptance_commands(
        commands=[_pytest_command(test_path)],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected=2,
    )

    assert results[0].passed is False
    assert results[0].classification == "acceptance_failed"
    assert results[0].pytest_counts["collected"] == 2
    assert results[0].pytest_counts["failed"] == 1


def test_floor_wins_over_acceptance_failure(tmp_path: Path) -> None:
    test_path = tmp_path / "test_failure.py"
    test_path.write_text(
        "def test_passes():\n"
        "    assert True\n\n"
        "def test_fails():\n"
        "    assert False\n",
        encoding="utf-8",
    )

    results = run_acceptance_commands(
        commands=[_pytest_command(test_path)],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected=3,
    )

    assert results[0].classification == "acceptance_collection_shrank"
    assert results[0].pytest_counts["failed"] == 1


def test_timeout_wins_over_collection_floor(tmp_path: Path) -> None:
    partial_pytest_output = "collected 1 item\n"
    command = _python_command(
        f"import time\nprint({partial_pytest_output!r}, end='', flush=True)\ntime.sleep(60)"
    )

    results = run_acceptance_commands(
        commands=[command],
        cwd=tmp_path,
        timeout_seconds=1,
        acceptance_min_collected=2,
    )

    assert results[0].classification == "acceptance_timeout"
    assert results[0].pytest_counts["collected"] == 1


def test_stale_import_binding_wins_over_collection_floor(tmp_path: Path) -> None:
    package = tmp_path / "src" / "only_in_worktree"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    command = shlex.join([sys.executable, "-m", "pytest", "tests"])

    results = run_acceptance_commands(
        commands=[command],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected=2,
        import_provenance_required=True,
    )

    assert results[0].classification == "stale_import_binding"
    assert results[0].pytest_counts == PYTEST_COUNTS_UNAVAILABLE


def test_per_command_floor_does_not_apply_to_unmapped_command(tmp_path: Path) -> None:
    test_path = _write_passing_tests(tmp_path)
    pytest_command = _pytest_command(test_path)
    ordinary_command = _python_command("print('ordinary successful output')")

    results = run_acceptance_commands(
        commands=[pytest_command, ordinary_command],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected={pytest_command: 2},
    )

    assert [result.classification for result in results] == ["success", "success"]
    assert results[1].pytest_counts == PYTEST_COUNTS_UNAVAILABLE


def test_run_summary_records_counts_beside_exit_code(tmp_path: Path) -> None:
    test_path = _write_passing_tests(tmp_path)
    results = run_acceptance_commands(
        commands=[_pytest_command(test_path)],
        cwd=tmp_path,
        timeout_seconds=10,
        acceptance_min_collected=2,
    )

    summary = _acceptance_command_summary(results)

    assert summary[0]["exit_code"] == 0
    assert summary[0]["pytest_counts"]["collected"] == 2
    assert summary[0]["acceptance_min_collected"] == 2


def _write_task_spec(tmp_path: Path, floor: object) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the task.", encoding="utf-8")
    command = f"{sys.executable} -m pytest tests"
    spec = {
        "id": "D-DENOMINATOR",
        "title": "Denominator test",
        "target_repo": str(repo),
        "cli": "codex",
        "prompt_template": str(prompt),
        "worktree_branch": "codex/denominator",
        "allowed_paths": ["**"],
        "acceptance": [command],
        "acceptance_min_collected": floor,
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


@pytest.mark.parametrize("floor", [12, {f"{sys.executable} -m pytest tests": 12}])
def test_task_spec_parses_global_and_per_command_floors(
    tmp_path: Path,
    floor: object,
) -> None:
    task = load_task(_write_task_spec(tmp_path, floor))

    assert task.acceptance_min_collected == floor


@pytest.mark.parametrize("floor", [-1, True, {"not an acceptance command": 1}])
def test_task_spec_rejects_invalid_or_unreachable_floors(
    tmp_path: Path,
    floor: object,
) -> None:
    with pytest.raises(ValueError, match="acceptance_min_collected"):
        load_task(_write_task_spec(tmp_path, floor))
