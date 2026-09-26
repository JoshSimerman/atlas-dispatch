"""The console entry point and its subcommands."""

from __future__ import annotations

import io
import subprocess
import sys
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from atlas_dispatch import cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def _invoke(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            rc = cli.main(args)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, stdout.getvalue(), stderr.getvalue()


def test_pyproject_declares_the_console_script() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"] == {"atlas-dispatch": "atlas_dispatch.cli:main"}


@pytest.mark.parametrize(
    "args",
    [["--help"], ["run", "--help"], ["doctor", "--help"], ["capabilities", "--help"]],
)
def test_help_is_reachable_for_every_command(args: list[str]) -> None:
    rc, stdout, stderr = _invoke(args)

    assert rc == 0
    assert stderr == ""
    assert "usage:" in stdout


@pytest.mark.parametrize("command", ["clis", "models"])
def test_listing_commands_print_the_registries(command: str) -> None:
    rc, stdout, stderr = _invoke([command])

    assert rc == 0
    assert stderr == ""
    assert "codex" in stdout
    assert "claude" in stdout


def test_capabilities_describes_a_registered_cli() -> None:
    rc, stdout, _ = _invoke(["capabilities", "codex"])

    assert rc == 0
    assert "argv_template:" in stdout
    assert "codex/gpt-6-sol-high" in stdout


def test_capabilities_rejects_an_unknown_cli() -> None:
    rc, _, stderr = _invoke(["capabilities", "not-a-cli"])

    assert rc == 1
    assert "unknown cli" in stderr


def test_unknown_subcommand_is_a_usage_error() -> None:
    rc, _, stderr = _invoke(["deploy"])

    assert rc == 2
    assert "invalid choice" in stderr


def test_python_dash_m_entry_point_works() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "atlas_dispatch", "clis"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "codex" in result.stdout
