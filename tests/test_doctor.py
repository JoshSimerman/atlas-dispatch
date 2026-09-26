"""``atlas-dispatch doctor`` is read-only and reports what the engine will use."""

from __future__ import annotations

import io

import pytest

from atlas_dispatch import doctor
from atlas_dispatch.cli import main as cli_main


def test_doctor_lists_every_registered_cli_and_the_protected_paths() -> None:
    out = io.StringIO()

    rc = doctor.run_doctor(stdout=out, environ={})

    text = out.getvalue()
    assert rc == 0
    for name in ("codex", "claude", "gemini", "kimi-code", "grok", "hermes"):
        assert f"- {name}\n" in text
    assert "git: ok" in text
    assert "- migrations/**" in text


def test_doctor_reports_an_active_command_override() -> None:
    out = io.StringIO()

    doctor.run_doctor(
        stdout=out,
        environ={"ATLAS_DISPATCH_CODEX_CMD": "/usr/bin/true"},
    )

    assert "env_override: /usr/bin/true" in out.getvalue()


def test_doctor_honours_a_protected_patterns_override() -> None:
    out = io.StringIO()

    doctor.run_doctor(
        stdout=out,
        environ={"ATLAS_DISPATCH_PROTECTED_PATTERNS": ""},
    )

    assert "(none configured)" in out.getvalue()


def test_doctor_fails_only_when_git_is_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "check_git", lambda: (False, "git was not found on PATH"))
    out = io.StringIO()

    assert doctor.run_doctor(stdout=out, environ={}) == 1
    assert "git: MISSING" in out.getvalue()


def test_cli_doctor_invokes_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def fake_run_doctor() -> int:
        called.append(True)
        return 7

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)

    assert cli_main(["doctor"]) == 7
    assert called == [True]
