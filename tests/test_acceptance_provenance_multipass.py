from __future__ import annotations

import os
import shlex
import venv
from pathlib import Path

import pytest

from atlas_dispatch import run_acceptance_commands
from atlas_dispatch.import_provenance import (
    IMPORT_PROVENANCE_STALE_CLASSIFICATION,
    IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION,
)


def _write_package(root: Path, name: str, value: str) -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f"VALUE = {value!r}\n",
        encoding="utf-8",
    )
    return package


def _create_venv(worktree: Path) -> Path:
    venv_dir = worktree / ".venv"
    venv.EnvBuilder(with_pip=False, system_site_packages=True, symlinks=True).create(venv_dir)
    return venv_dir / "bin" / "python"


def _write_pytest_launcher(venv_python: Path) -> Path:
    launcher = venv_python.parent / "pytest"
    launcher.write_text(
        f"#!{venv_python}\n"
        "from pytest import console_main\n"
        "raise SystemExit(console_main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _write_test(worktree: Path, filename: str, package: str, value: str) -> Path:
    tests_dir = worktree / "tests"
    tests_dir.mkdir(exist_ok=True)
    test_path = tests_dir / filename
    test_path.write_text(
        f"import {package}\n\n"
        "def test_binding():\n"
        f"    assert {package}.VALUE == {value!r}\n",
        encoding="utf-8",
    )
    return test_path


def _write_local_pytest_module(worktree: Path) -> None:
    (worktree / "pytest.py").write_text(
        "from __future__ import annotations\n\n"
        "import runpy\n"
        "import sys\n\n"
        "def console_main() -> int:\n"
        "    for arg in sys.argv[1:]:\n"
        "        if not arg.endswith('.py'):\n"
        "            continue\n"
        "        namespace = runpy.run_path(arg)\n"
        "        for name, value in namespace.items():\n"
        "            if name.startswith('test_') and callable(value):\n"
        "                value()\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(console_main())\n",
        encoding="utf-8",
    )


def _package_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    worktree = tmp_path / "worktree"
    local_src = worktree / "src"
    foreign_src = tmp_path / "foreign"
    _write_package(local_src, "multipass_pkg", "local")
    _write_package(foreign_src, "multipass_pkg", "foreign")
    _write_local_pytest_module(worktree)
    venv_python = _create_venv(worktree)
    return worktree, local_src, foreign_src, venv_python


def test_second_import_bearing_command_is_probed_and_stale_binding_refuses(
    tmp_path: Path,
) -> None:
    worktree, local_src, foreign_src, venv_python = _package_fixture(tmp_path)
    _write_test(worktree, "test_local.py", "multipass_pkg", "local")
    execution_marker = worktree / "stale-command-ran"
    foreign_test = _write_test(
        worktree,
        "test_foreign.py",
        "multipass_pkg",
        "foreign",
    )
    foreign_test.write_text(
        "from pathlib import Path\n"
        f"Path({str(execution_marker)!r}).touch()\n"
        + foreign_test.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    first = (
        f"PYTHONPATH={shlex.quote(str(local_src))} "
        f"{shlex.quote(str(venv_python))} -m pytest tests/test_local.py -q"
    )
    second = (
        f"PYTHONPATH={shlex.quote(os.pathsep.join([str(foreign_src), str(local_src)]))} "
        f"{shlex.quote(str(venv_python))} -m pytest tests/test_foreign.py -q"
    )
    later_marker = worktree / "later-command-ran"
    marker_code = f"open({str(later_marker)!r}, 'w').close()"
    later = (
        f"{shlex.quote(str(venv_python))} -c {shlex.quote(marker_code)}"
    )

    results = run_acceptance_commands(
        commands=[first, second, later],
        cwd=worktree,
        import_provenance_required=True,
    )

    assert results[0].name == first
    assert results[0].passed
    assert results[1].name == "import provenance guard"
    assert not results[1].passed
    assert results[1].classification == IMPORT_PROVENANCE_STALE_CLASSIFICATION
    assert results.exit_codes == [0, 1]
    assert not execution_marker.exists()
    assert not later_marker.exists()


def test_uv_run_pytest_is_probed_and_stale_binding_refuses(tmp_path: Path) -> None:
    worktree, local_src, foreign_src, venv_python = _package_fixture(tmp_path)
    _write_test(worktree, "test_foreign.py", "multipass_pkg", "foreign")
    _write_pytest_launcher(venv_python)
    tool_dir = worktree / "tools"
    tool_dir.mkdir()
    uv = tool_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'test "$1" = "run" || exit 64\n'
        "shift\n"
        'test "$1" != "--frozen" || shift\n'
        'exec "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    path = os.pathsep.join([str(tool_dir), str(venv_python.parent), os.environ["PATH"]])
    pythonpath = os.pathsep.join([str(foreign_src), str(local_src)])
    command = (
        f"PATH={shlex.quote(path)} PYTHONPATH={shlex.quote(pythonpath)} "
        "uv run --frozen pytest tests/test_foreign.py -q"
    )

    results = run_acceptance_commands(
        commands=[command],
        cwd=worktree,
        import_provenance_required=True,
    )

    assert len(results) == 1
    assert results[0].name == "import provenance guard"
    assert results[0].classification == IMPORT_PROVENANCE_STALE_CLASSIFICATION
    assert results.exit_codes == [1]


def test_uv_default_expansion_pytest_is_unknown_and_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, local_src, foreign_src, venv_python = _package_fixture(tmp_path)
    _write_test(worktree, "test_foreign.py", "multipass_pkg", "foreign")
    _write_pytest_launcher(venv_python)
    tool_dir = worktree / "tools"
    tool_dir.mkdir()
    execution_marker = worktree / "uv-command-ran"
    uv = tool_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f"touch {shlex.quote(str(execution_marker))}\n"
        'test "$1" = "run" || exit 64\n'
        "shift\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    path = os.pathsep.join([str(tool_dir), str(venv_python.parent), os.environ["PATH"]])
    pythonpath = os.pathsep.join([str(foreign_src), str(local_src)])
    command = (
        f"PATH={shlex.quote(path)} PYTHONPATH={shlex.quote(pythonpath)} "
        "uv run ${RUNNER:-pytest} tests/test_foreign.py -q"
    )
    monkeypatch.delenv("RUNNER", raising=False)

    results = run_acceptance_commands(
        commands=[command],
        cwd=worktree,
        import_provenance_required=True,
    )

    assert len(results) == 1
    assert results[0].name == "import provenance guard"
    assert results[0].classification == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION
    assert "could not be resolved" in results[0].details
    assert results.exit_codes == [1]
    assert not execution_marker.exists()


def test_multiflag_bash_pytest_with_inner_environment_is_probed(
    tmp_path: Path,
) -> None:
    worktree, local_src, foreign_src, venv_python = _package_fixture(tmp_path)
    _write_test(worktree, "test_foreign.py", "multipass_pkg", "foreign")
    _write_pytest_launcher(venv_python)
    pythonpath = os.pathsep.join([str(foreign_src), str(local_src)])
    inner_command = (
        f"PATH={shlex.quote(str(venv_python.parent))} "
        f"PYTHONPATH={shlex.quote(pythonpath)} "
        "pytest tests/test_foreign.py -q"
    )
    command = f"/bin/bash -euxo pipefail -c {shlex.quote(inner_command)}"

    results = run_acceptance_commands(
        commands=[command],
        cwd=worktree,
        import_provenance_required=True,
    )

    assert len(results) == 1
    assert results[0].name == "import provenance guard"
    assert results[0].classification == IMPORT_PROVENANCE_STALE_CLASSIFICATION
    assert results.exit_codes == [1]


@pytest.mark.parametrize(
    "uv_command",
    [
        "uv run pytest tests -q",
        "uv run --group dev pytest tests -q",
    ],
)
def test_unresolvable_uv_interpreter_is_unknown_and_refused(
    tmp_path: Path,
    uv_command: str,
) -> None:
    worktree = tmp_path / "worktree"
    _write_package(worktree / "src", "multipass_pkg", "local")
    command = f"PATH=/definitely/not/a/real/path {uv_command}"

    results = run_acceptance_commands(
        commands=[command],
        cwd=worktree,
        import_provenance_required=True,
    )

    assert len(results) == 1
    assert results[0].name == "import provenance guard"
    assert results[0].classification == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION
    assert "could not be resolved" in results[0].details
    assert results.exit_codes == [1]


def test_ambiguous_pytest_bearing_bash_wrapper_is_unknown_and_refused(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    _write_package(worktree / "src", "multipass_pkg", "local")
    command = "/bin/bash -e pytest tests -q"

    results = run_acceptance_commands(
        commands=[command],
        cwd=worktree,
        import_provenance_required=True,
    )

    assert len(results) == 1
    assert results[0].name == "import provenance guard"
    assert results[0].classification == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION
    assert "could not be resolved" in results[0].details
    assert results.exit_codes == [1]


def test_single_clean_venv_pytest_command_is_probed_and_runs(tmp_path: Path) -> None:
    worktree, local_src, _foreign_src, venv_python = _package_fixture(tmp_path)
    _write_test(worktree, "test_local.py", "multipass_pkg", "local")
    command = (
        f"PYTHONPATH={shlex.quote(str(local_src))} "
        f"{shlex.quote(str(venv_python))} -m pytest tests/test_local.py -q"
    )

    results = run_acceptance_commands(
        commands=[command],
        cwd=worktree,
        import_provenance_required=True,
    )

    assert len(results) == 1
    assert results[0].name == command
    assert results[0].passed
    assert results.exit_codes == [0]
