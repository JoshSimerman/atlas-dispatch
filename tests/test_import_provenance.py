from __future__ import annotations

import json
import shlex
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

import atlas_dispatch.dispatcher as dispatcher_mod
from atlas_dispatch.adapter import AdapterResult, Classification, DispatchErrorKind
from atlas_dispatch.import_provenance import (
    IMPORT_PROVENANCE_REMEDY,
    IMPORT_PROVENANCE_STALE_CLASSIFICATION,
    IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION,
    ImportProvenanceResult,
    PackageImportResult,
    _acceptance_command_argv,
    _argv_is_direct_pytest_invocation,
    _declared_build_package_dirs,
    _uv_run_parts,
    discover_import_packages,
    format_import_provenance_failure,
    is_import_bearing_acceptance_command,
    run_import_provenance_check,
)
from atlas_dispatch.verify import DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS, CheckResult
from atlas_dispatch.worktree import Worktree, worktree_path_for


def _write_pkg(root: Path, name: str, body: str = "") -> Path:
    package_dir = root / name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(body, encoding="utf-8")
    return package_dir


def _write_hatch_pyproject(worktree: Path, package_name: str) -> None:
    (worktree / "pyproject.toml").write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["hatchling"]',
                'build-backend = "hatchling.build"',
                "",
                "[tool.hatch.build.targets.wheel]",
                f'packages = ["{package_name}"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_setuptools_pyproject(
    worktree: Path,
    *,
    packages: list[str] | None = None,
    package_dir: dict[str, str] | None = None,
    find_where: list[str] | None = None,
) -> None:
    lines = [
        "[build-system]",
        'requires = ["setuptools"]',
        'build-backend = "setuptools.build_meta"',
        "",
    ]
    if packages is not None:
        lines.extend(
            [
                "[tool.setuptools]",
                f"packages = {json.dumps(packages)}",
            ]
        )
        if package_dir is not None:
            mappings = ", ".join(
                f"{json.dumps(name)} = {json.dumps(path)}"
                for name, path in package_dir.items()
            )
            lines.append(f"package-dir = {{{mappings}}}")
        lines.append("")
    if find_where is not None:
        lines.extend(
            [
                "[tool.setuptools.packages.find]",
                f"where = {json.dumps(find_where)}",
                "",
            ]
        )
    (worktree / "pyproject.toml").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_dispatch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    return repo


def _create_venv(worktree: Path) -> Path:
    venv_dir = worktree / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_dir)
    return venv_dir / "bin" / "python"


def _site_packages(venv_python: Path) -> Path:
    result = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _is_import_provenance_probe_args(args: object) -> bool:
    return (
        isinstance(args, list)
        and len(args) >= 3
        and args[1] == "-c"
        and dispatcher_mod.import_provenance.PROBE_STDOUT_PREFIX in args[2]
    )


def _successful_adapter_result() -> AdapterResult:
    return AdapterResult(
        cli="codex",
        exit_code=0,
        stdout="done",
        stderr="",
        duration_seconds=0.0,
        command=["codex"],
        classification=Classification(
            kind=DispatchErrorKind.SUCCESS,
            suggested_action="",
        ),
    )


@pytest.mark.parametrize(
    "command",
    [
        "uv run --frozen pytest tests -q",
        "uv run --group dev pytest tests -q",
        "uv run --python 3.12 pytest tests -q",
        '/bin/bash -e -c "pytest tests -q"',
        '/bin/bash -euxo pipefail -c "pytest tests -q"',
        '/bin/bash -lc "uv run --frozen pytest tests -q"',
    ],
)
def test_flagged_pytest_wrappers_are_import_bearing(command: str) -> None:
    assert is_import_bearing_acceptance_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "uv run ${RUNNER:-pytest} tests -q",
        "uv run ${RUNNER-pytest} tests -q",
        "/bin/bash -c '${RUNNER:-pytest} tests -q'",
    ],
)
def test_shell_default_expansion_pytest_wrappers_are_import_bearing(
    command: str,
) -> None:
    assert is_import_bearing_acceptance_command(command)


def _write_task_spec(tmp_path: Path, worktree: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the task.", encoding="utf-8")
    spec = {
        "id": "D-PROVENANCE",
        "title": "Import provenance test",
        "target_repo": str(repo),
        "cli": "codex",
        "prompt_template": str(prompt),
        "worktree_branch": "codex/provenance",
        "allowed_paths": ["**"],
        "runs_dir": str(tmp_path / "runs"),
        "acceptance": [".venv/bin/python -m pytest tests -q"],
    }
    spec_path = tmp_path / "task.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    worktree.mkdir(exist_ok=True)
    return spec_path


def _write_dispatch_task_spec(
    tmp_path: Path,
    extra: dict[str, object],
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the task.", encoding="utf-8")
    spec: dict[str, object] = {
        "id": "D-PROVENANCE",
        "title": "Import provenance test",
        "target_repo": str(repo),
        "cli": "codex",
        "prompt_template": str(prompt),
        "worktree_branch": "codex/provenance",
        "allowed_paths": ["**"],
    }
    spec.update(extra)
    spec_path = tmp_path / "task.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path


def test_detects_real_stale_binding_from_venv_pth(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    local_src = worktree / "libs" / "core" / "src"
    foreign_src = tmp_path / "foreign"
    _write_pkg(local_src, "provenance_pkg", "VALUE = 'local'\n")
    foreign_pkg = _write_pkg(foreign_src, "provenance_pkg", "VALUE = 'foreign'\n")
    venv_python = _create_venv(worktree)
    pth_path = _site_packages(venv_python) / "stale_provenance_pkg.pth"
    pth_path.write_text(f"{foreign_src}\n{local_src}\n", encoding="utf-8")
    run_dir = tmp_path / "run"

    result = run_import_provenance_check(
        worktree,
        acceptance_command=".venv/bin/python -m pytest tests -q",
        run_dir=run_dir,
    )

    assert result.verdict == IMPORT_PROVENANCE_STALE_CLASSIFICATION
    assert len(result.packages) == 1
    package = result.packages[0]
    assert package.name == "provenance_pkg"
    assert package.verdict == "outside_worktree"
    assert package.resolved_path == str((foreign_pkg / "__init__.py").resolve())
    artifact = json.loads((run_dir / "import_provenance.json").read_text())
    assert artifact["verdict"] == IMPORT_PROVENANCE_STALE_CLASSIFICATION
    assert artifact["packages"][0]["name"] == "provenance_pkg"
    assert artifact["packages"][0]["resolved_path"] == package.resolved_path
    assert artifact["packages"][0]["verdict"] == "outside_worktree"


def test_all_packages_inside_worktree_allows_dispatch_acceptance(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    spec_path = _write_task_spec(tmp_path, worktree)
    _write_pkg(worktree, "local_pkg", "VALUE = 'local'\n")
    _write_hatch_pyproject(worktree, "local_pkg")
    _create_venv(worktree)
    acceptance = Mock(
        return_value=[
            CheckResult(name=".venv/bin/python -m pytest tests -q", passed=True)
        ]
    )

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree,
                branch="codex/provenance",
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", return_value=_successful_adapter_result()),
        patch(
            "atlas_dispatch.dispatcher._capture_orchestrator_refs",
            return_value={
                "refs": {
                    "main": "0" * 40,
                    "HEAD": "0" * 40,
                    "origin/main": "0" * 40,
                },
                "errors": {},
            },
        ),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["local_pkg/__init__.py"],
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
        patch("atlas_dispatch.dispatcher.run_acceptance_commands", acceptance),
    ):
        rc = dispatcher_mod.dispatch(spec_path)

    assert rc == 0
    acceptance.assert_called_once_with(
        commands=[".venv/bin/python -m pytest tests -q"],
        cwd=worktree,
        timeout_seconds=DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS,
        import_provenance_required=True,
        run_dir=next((tmp_path / "runs").iterdir()),
    )


def test_flat_layout_without_build_declaration_stays_unknown_not_pass(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "flat-layout"
    worktree.mkdir()
    package_dir = _write_pkg(worktree, "flat_pkg", "VALUE = 'importable'\n")
    run_dir = tmp_path / "run"

    venv_python = _create_venv(worktree)
    import_result = subprocess.run(
        [str(venv_python), "-c", "import flat_pkg; print(flat_pkg.__file__)"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    result = run_import_provenance_check(
        worktree,
        acceptance_command=f"{shlex.quote(str(venv_python))} -m pytest tests -q",
        run_dir=run_dir,
    )

    assert import_result.stdout.strip() == str(package_dir / "__init__.py")
    assert not (worktree / "pyproject.toml").exists()
    assert result.verdict == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION
    assert not result.passed
    assert result.packages == []
    artifact = json.loads((run_dir / "import_provenance.json").read_text())
    assert artifact["verdict"] == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION
    assert "no top-level packages discovered" in artifact["reason"]


def test_import_error_is_unresolvable_and_names_package(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_pkg(worktree, "broken_pkg", "raise ImportError('boom')\n")
    _write_hatch_pyproject(worktree, "broken_pkg")

    venv_python = _create_venv(worktree)
    result = run_import_provenance_check(
        worktree,
        acceptance_command=f"{shlex.quote(str(venv_python))} -m pytest tests -q",
    )

    assert result.verdict == IMPORT_PROVENANCE_STALE_CLASSIFICATION
    assert result.packages[0].name == "broken_pkg"
    assert result.packages[0].verdict == "import_error"
    assert "ImportError: boom" in (result.packages[0].error or "")


def test_probe_uses_worktree_venv_python_when_present(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_pkg(worktree, "local_pkg", "")
    _write_hatch_pyproject(worktree, "local_pkg")
    venv_python = worktree / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    captured: dict[str, list[str]] = {}
    payload = {
        "results": [
            {
                "name": "local_pkg",
                "file": str(worktree / "local_pkg" / "__init__.py"),
                "error": None,
            }
        ]
    }

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=dispatcher_mod.import_provenance.PROBE_STDOUT_PREFIX
            + json.dumps(payload)
            + "\n",
            stderr="",
        )

    monkeypatch.setattr(dispatcher_mod.import_provenance.subprocess, "run", fake_run)

    result = run_import_provenance_check(
        worktree,
        acceptance_command=".venv/bin/python -m pytest tests -q",
    )

    assert result.passed
    assert captured["args"][0] == str(venv_python)


def test_stale_binding_refuses_acceptance_without_mutating_venv_or_raising(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    spec_path = _write_task_spec(tmp_path, worktree)
    local_src = worktree / "libs" / "core" / "src"
    foreign_src = tmp_path / "foreign"
    _write_pkg(local_src, "provenance_pkg", "VALUE = 'local'\n")
    _write_pkg(foreign_src, "provenance_pkg", "VALUE = 'foreign'\n")
    venv_python = _create_venv(worktree)
    pth_path = _site_packages(venv_python) / "stale_provenance_pkg.pth"
    pth_text = f"{foreign_src}\n{local_src}\n"
    pth_path.write_text(pth_text, encoding="utf-8")
    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree,
                branch="codex/provenance",
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", return_value=_successful_adapter_result()),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["libs/core/src/provenance_pkg/__init__.py"],
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        rc = dispatcher_mod.dispatch(spec_path)

    assert rc == 1
    assert pth_path.read_text(encoding="utf-8") == pth_text
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["acceptance_commands"][0]["classification"] == (
        IMPORT_PROVENANCE_STALE_CLASSIFICATION
    )
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Import provenance guard refused to run acceptance" in report
    assert "provenance_pkg" in report
    assert IMPORT_PROVENANCE_REMEDY in report


def test_dispatcher_builds_venv_before_import_provenance_probe(
    tmp_path: Path,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    package_dir = repo / "libs" / "core" / "src" / "sequenced_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 'local'\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sequenced_pkg.py").write_text(
        "import sequenced_pkg\n\n"
        "def test_value():\n"
        "    assert sequenced_pkg.VALUE == 'local'\n",
        encoding="utf-8",
    )
    # Keep the command pytest-shaped for interpreter resolution without requiring
    # the nested acceptance venv to inherit pytest from this test runner's venv.
    (repo / "pytest.py").write_text(
        "import sequenced_pkg\n\n"
        "assert sequenced_pkg.VALUE == 'local'\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add package")
    spec_path = _write_dispatch_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/provenance-sequencing",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": [
                f"{shlex.quote(sys.executable)} -m venv --system-site-packages "
                "--without-pip .venv",
                ".venv/bin/python -c "
                + shlex.quote(
                    "import sysconfig\n"
                    "from pathlib import Path\n"
                    "Path(sysconfig.get_paths()['purelib'], "
                    "'sequenced_pkg.pth').write_text("
                    "str(Path.cwd() / 'libs' / 'core' / 'src'), "
                    "encoding='utf-8')\n"
                ),
                ".venv/bin/python -m pytest tests -q",
            ],
        },
    )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return _successful_adapter_result()

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatcher_mod.dispatch(spec_path)

    assert rc == 0
    run_dir = next((tmp_path / "runs").iterdir())
    artifact = json.loads((run_dir / "import_provenance.json").read_text())
    worktree = worktree_path_for(repo, "codex/provenance-sequencing")
    assert artifact["interpreter"] == str(worktree / ".venv" / "bin" / "python")
    assert artifact["verdict"] == "pass"
    assert artifact["packages"][0]["name"] == "sequenced_pkg"


def test_dispatcher_targets_venv_accept_python_from_acceptance_command(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    package_dir = repo / "libs" / "core" / "src" / "venv_accept_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 'local'\n", encoding="utf-8")
    # Keep the command pytest-shaped for interpreter resolution without requiring
    # the isolated acceptance venv to have the real pytest package installed.
    (repo / "pytest.py").write_text(
        "import venv_accept_pkg\n\n"
        "assert venv_accept_pkg.VALUE == 'local'\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add venv accept package")
    spec_path = _write_dispatch_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/provenance-venv-accept",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": [
                f"{shlex.quote(sys.executable)} -m venv --without-pip .venv-accept",
                ".venv-accept/bin/python -c "
                + shlex.quote(
                    "import sysconfig\n"
                    "from pathlib import Path\n"
                    "Path(sysconfig.get_paths()['purelib'], "
                    "'venv_accept_pkg.pth').write_text("
                    "str(Path.cwd() / 'libs' / 'core' / 'src'), "
                    "encoding='utf-8')\n"
                ),
                ".venv-accept/bin/python -m pytest",
            ],
        },
    )
    captured_args: list[str] = []
    real_subprocess_run = subprocess.run

    def fake_probe(args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not _is_import_provenance_probe_args(args):
            return real_subprocess_run(args, **kwargs)
        assert isinstance(args, list)
        captured_args[:] = args
        worktree = worktree_path_for(repo, "codex/provenance-venv-accept")
        assert (worktree / ".venv-accept" / "bin" / "python").exists()
        payload = {
            "results": [
                {
                    "name": "venv_accept_pkg",
                    "file": str(
                        worktree
                        / "libs"
                        / "core"
                        / "src"
                        / "venv_accept_pkg"
                        / "__init__.py"
                    ),
                    "error": None,
                }
            ]
        }
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=dispatcher_mod.import_provenance.PROBE_STDOUT_PREFIX
            + json.dumps(payload)
            + "\n",
            stderr="",
        )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return _successful_adapter_result()

    monkeypatch.setattr(dispatcher_mod.import_provenance.subprocess, "run", fake_probe)
    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatcher_mod.dispatch(spec_path)

    worktree = worktree_path_for(repo, "codex/provenance-venv-accept")
    assert rc == 0
    assert captured_args[0] == str(worktree / ".venv-accept" / "bin" / "python")
    assert captured_args[0] != sys.executable


def test_unresolved_acceptance_interpreter_is_unknown_without_sys_executable_probe(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    package_dir = repo / "libs" / "core" / "src" / "unknown_interp_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add package")
    spec_path = _write_dispatch_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/provenance-unknown-interpreter",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["pytest tests -q"],
        },
    )
    captured_args: list[str] = []
    real_subprocess_run = subprocess.run

    def fake_probe(args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not _is_import_provenance_probe_args(args):
            return real_subprocess_run(args, **kwargs)
        assert isinstance(args, list)
        captured_args[:] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return _successful_adapter_result()

    monkeypatch.setattr(dispatcher_mod.import_provenance.subprocess, "run", fake_probe)
    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatcher_mod.dispatch(spec_path)

    assert rc == 1
    assert sys.executable not in captured_args
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["acceptance_commands"][0]["classification"] == "unknown"
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "acceptance interpreter could not be resolved" in report
    assert "This is unknown, not a pass." in report


def test_stale_binding_after_setup_blocks_later_acceptance_commands(
    tmp_path: Path,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    local_src = repo / "libs" / "core" / "src"
    local_pkg = local_src / "stale_after_setup_pkg"
    local_pkg.mkdir(parents=True)
    (local_pkg / "__init__.py").write_text("VALUE = 'local'\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_stale_after_setup_pkg.py").write_text(
        "import stale_after_setup_pkg\n\n"
        "def test_value():\n"
        "    assert stale_after_setup_pkg.VALUE == 'local'\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add stale package")
    foreign_src = tmp_path / "foreign"
    foreign_pkg = foreign_src / "stale_after_setup_pkg"
    foreign_pkg.mkdir(parents=True)
    (foreign_pkg / "__init__.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
    marker = "later-command-ran.txt"
    spec_path = _write_dispatch_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/provenance-stale-after-setup",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": [
                f"{shlex.quote(sys.executable)} -m venv --system-site-packages "
                "--without-pip .venv",
                ".venv/bin/python -c "
                + shlex.quote(
                    "import sysconfig\n"
                    "from pathlib import Path\n"
                    f"foreign = Path({str(foreign_src)!r})\n"
                    "local = Path.cwd() / 'libs' / 'core' / 'src'\n"
                    "Path(sysconfig.get_paths()['purelib'], "
                    "'stale_after_setup_pkg.pth').write_text("
                    "f'{foreign}\\n{local}\\n', encoding='utf-8')\n"
                ),
                ".venv/bin/python -m pytest tests -q",
                f"touch {marker}",
            ],
        },
    )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return _successful_adapter_result()

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatcher_mod.dispatch(spec_path)

    worktree = worktree_path_for(repo, "codex/provenance-stale-after-setup")
    assert rc == 1
    assert (worktree / ".venv" / "bin" / "python").exists()
    assert not (worktree / marker).exists()
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert [item["passed"] for item in summary["acceptance_commands"]] == [
        True,
        True,
        False,
    ]
    assert summary["acceptance_commands"][-1]["classification"] == (
        IMPORT_PROVENANCE_STALE_CLASSIFICATION
    )


def test_import_provenance_artifact_contains_package_paths_and_verdict(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree, "local_pkg", "")
    _write_hatch_pyproject(worktree, "local_pkg")
    run_dir = tmp_path / "run"

    venv_python = _create_venv(worktree)
    result = run_import_provenance_check(
        worktree,
        acceptance_command=f"{shlex.quote(str(venv_python))} -m pytest tests -q",
        run_dir=run_dir,
    )

    assert result.passed
    artifact = json.loads((run_dir / "import_provenance.json").read_text())
    assert artifact["verdict"] == "pass"
    assert artifact["packages"] == [
        {
            "name": "local_pkg",
            "source_paths": [str(package_dir.resolve())],
            "resolved_path": str((package_dir / "__init__.py").resolve()),
            "inside_worktree": True,
            "verdict": "inside_worktree",
            "error": None,
        }
    ]


def test_failure_details_include_exact_remedy_and_resolved_path() -> None:
    result = ImportProvenanceResult(
        worktree_path="/tmp/worktree",
        interpreter="/tmp/worktree/.venv/bin/python",
        verdict=IMPORT_PROVENANCE_STALE_CLASSIFICATION,
        reason="one or more packages did not resolve inside the worktree",
        packages=[
            PackageImportResult(
                name="foo",
                source_paths=["/tmp/worktree/libs/foo/src/foo"],
                resolved_path="/tmp/foreign/foo/__init__.py",
                inside_worktree=False,
                verdict="outside_worktree",
            )
        ],
        probe_return_code=0,
    )

    details = format_import_provenance_failure(result)

    assert "Import provenance guard refused to run acceptance" in details
    assert f"Remedy: {IMPORT_PROVENANCE_REMEDY}." in details
    assert "- foo: resolved_path=/tmp/foreign/foo/__init__.py verdict=outside_worktree" in details


def test_discovers_declared_root_layout_package_for_atlas_dispatch_style(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree, "atlas_dispatch", "")
    _write_hatch_pyproject(worktree, "atlas_dispatch")

    packages = discover_import_packages(worktree)

    assert [(package.name, package.source_paths) for package in packages] == [
        ("atlas_dispatch", [str(package_dir.resolve())])
    ]


def test_setuptools_declared_root_package_is_discovered_and_verified(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree, "pkg", "VALUE = 'local'\n")
    _write_setuptools_pyproject(worktree, packages=["pkg"])
    venv_python = _create_venv(worktree)

    result = run_import_provenance_check(
        worktree,
        acceptance_command=f"{shlex.quote(str(venv_python))} -m pytest tests -q",
    )

    assert result.passed
    assert [(package.name, package.source_paths) for package in result.packages] == [
        ("pkg", [str(package_dir.resolve())])
    ]


def test_setuptools_declared_missing_directory_stays_unknown(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_setuptools_pyproject(worktree, packages=["missing_pkg"])

    result = run_import_provenance_check(worktree)

    assert result.verdict == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION
    assert result.packages == []


def test_setuptools_declared_directory_without_init_stays_unknown(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "not_a_package").mkdir()
    _write_setuptools_pyproject(worktree, packages=["not_a_package"])

    result = run_import_provenance_check(worktree)

    assert result.verdict == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION
    assert result.packages == []


def test_setuptools_root_package_dir_resolves_name_below_src(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree / "src", "pkg_x")
    _write_setuptools_pyproject(
        worktree,
        packages=["pkg_x"],
        package_dir={"": "src"},
    )

    packages = discover_import_packages(worktree)
    declared_packages = _declared_build_package_dirs(worktree.resolve())

    assert not (worktree / "pkg_x").exists()
    assert declared_packages == [("pkg_x", package_dir)]
    assert [(package.name, package.source_paths) for package in packages] == [
        ("pkg_x", [str(package_dir.resolve())])
    ]


def test_setuptools_named_package_dir_resolves_declared_name(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree / "lib", "renamed")
    _write_setuptools_pyproject(
        worktree,
        packages=["public_name"],
        package_dir={"public_name": "lib/renamed"},
    )

    packages = discover_import_packages(worktree)

    assert [(package.name, package.source_paths) for package in packages] == [
        ("public_name", [str(package_dir.resolve())])
    ]


def test_setuptools_dotted_package_name_resolves_to_nested_directory(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_pkg(worktree, "a")
    package_dir = _write_pkg(worktree / "a", "b")
    _write_setuptools_pyproject(worktree, packages=["a.b"])

    packages = discover_import_packages(worktree)

    assert [(package.name, package.source_paths) for package in packages] == [
        ("a.b", [str(package_dir.resolve())])
    ]


def test_setuptools_find_where_discovers_packages_below_search_root(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree / "python", "found_pkg")
    _write_setuptools_pyproject(worktree, find_where=["python"])

    packages = discover_import_packages(worktree)

    assert [(package.name, package.source_paths) for package in packages] == [
        ("found_pkg", [str(package_dir.resolve())])
    ]


def test_hatch_declared_src_package_remains_path_based(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree / "src", "app_core")
    _write_hatch_pyproject(worktree, "src/app_core")

    packages = discover_import_packages(worktree)

    assert [(package.name, package.source_paths) for package in packages] == [
        ("app_core", [str(package_dir.resolve())])
    ]


def test_hatch_and_setuptools_declarations_union_without_duplicates(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    package_dir = _write_pkg(worktree, "shared_pkg")
    (worktree / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.hatch.build.targets.wheel]",
                'packages = ["shared_pkg"]',
                "",
                "[tool.setuptools]",
                'packages = ["shared_pkg"]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    packages = discover_import_packages(worktree)

    assert [(package.name, package.source_paths) for package in packages] == [
        ("shared_pkg", [str(package_dir.resolve())])
    ]


def test_ruff_only_acceptance_does_not_require_import_provenance() -> None:
    assert not dispatcher_mod._acceptance_requires_import_provenance(
        [".venv/bin/ruff check atlas_dispatch/import_provenance.py"]
    )


def test_real_flat_atlas_dispatch_worktree_import_provenance_passes() -> None:
    worktree = Path.cwd()
    if not (worktree / ".venv" / "bin" / "python").exists():
        pytest.skip(
            "needs this checkout's own .venv with atlas-dispatch installed editable "
            "(python3 -m venv .venv && .venv/bin/pip install -e . pytest)"
        )
    result = run_import_provenance_check(
        worktree,
        acceptance_command=".venv/bin/python -m pytest tests -q",
    )

    assert result.passed
    assert result.interpreter == str(worktree / ".venv" / "bin" / "python")
    assert [package.name for package in result.packages] == ["atlas_dispatch"]


# --- uv option-with-value table ------------------------------------------
# Without `--extra` in UV_RUN_OPTIONS_WITH_VALUES, `uv run --extra dev python -m
# pytest …` parses `dev` as the executable. The guard then returns `unknown`
# (correctly NOT a pass), and a build that had already succeeded is recorded as
# failed with its acceptance tests never executed. Failing closed is right; the
# guard must still read the command correctly.


@pytest.mark.parametrize(
    "command",
    [
        "uv run --extra dev python -m pytest -q libs/x/tests/test_a.py",
        "uv run --extra dev pytest -q tests/test_a.py",
        "uv run --extra dev --group lint python -m pytest -q tests/test_a.py",
        "uv run --python 3.12 python -m pytest -q tests/test_a.py",
        "uv run --project . pytest -q tests/test_a.py",
    ],
)
def test_uv_options_with_values_do_not_swallow_the_executable(command):
    argv = _acceptance_command_argv(command)
    parts = _uv_run_parts(argv)
    assert parts is not None, command
    _options, inner = parts
    assert _argv_is_direct_pytest_invocation(inner), (command, inner)


def test_valueless_uv_flag_still_leaves_the_executable_in_place():
    """The mirror case: a bare flag must NOT be treated as consuming a value, or
    the fix for --extra would break every command using --frozen/--locked."""
    for command in (
        "uv run --frozen pytest -q tests/test_a.py",
        "uv run --locked python -m pytest -q tests/test_a.py",
    ):
        argv = _acceptance_command_argv(command)
        parts = _uv_run_parts(argv)
        assert parts is not None, command
        _options, inner = parts
        assert _argv_is_direct_pytest_invocation(inner), (command, inner)
