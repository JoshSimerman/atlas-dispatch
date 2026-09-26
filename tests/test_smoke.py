"""Smoke tests — exercise public surface without subprocess'ing real CLIs."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from atlas_dispatch import (
    CLIS,
    MODELS,
    AdapterResult,
    Classification,
    DispatchErrorKind,
    ModelDefinition,
    classify_result,
    list_supported_clis,
    list_supported_models,
    render_command,
    render_prompt,
    resolve_model,
)
from atlas_dispatch.dispatcher import TaskSpec

# --------------------------------------------------------------------------- #
# Registries                                                                  #
# --------------------------------------------------------------------------- #


def test_public_surface_exposes_core_types() -> None:
    assert isinstance(Classification, type)
    assert isinstance(ModelDefinition, type)


def test_clis_registry_has_required_entries() -> None:
    assert {"codex", "claude", "gemini", "kimi", "kimi-code"}.issubset(set(list_supported_clis()))
    for name in list_supported_clis():
        definition = CLIS[name]
        assert definition.executable
        assert definition.argv_template
        assert definition.auth_setup_hint


def test_models_registry_resolves_codex_xhigh() -> None:
    definition = resolve_model("codex/gpt-6-sol-xhigh")
    assert definition.cli == "codex"
    assert definition.model_id == "gpt-6-sol"
    assert definition.reasoning_effort == "xhigh"


def test_resolve_model_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        resolve_model("nonexistent/model")


def test_every_model_references_a_known_cli() -> None:
    cli_names = set(list_supported_clis())
    for model_name in list_supported_models():
        definition = MODELS[model_name]
        assert definition.cli in cli_names, (
            f"model {model_name} references unknown cli {definition.cli}"
        )


# --------------------------------------------------------------------------- #
# render_command                                                              #
# --------------------------------------------------------------------------- #


def test_render_command_substitutes_placeholders(tmp_path: Path) -> None:
    cmd = render_command(
        cli="codex",
        cwd=tmp_path,
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )
    joined = " ".join(cmd)
    assert "gpt-5.5" in joined
    assert "xhigh" in joined
    assert str(tmp_path) in joined
    assert "{model}" not in joined
    assert "{cwd}" not in joined


def test_render_command_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "ATLAS_DISPATCH_GEMINI_CMD",
        "gemini --some-flag --cwd {cwd} --model {model}",
    )
    cmd = render_command(cli="gemini", cwd=tmp_path, model="gemini-2.5-pro")
    joined = " ".join(cmd)
    assert "--some-flag" in joined
    assert "gemini-2.5-pro" in joined
    assert str(tmp_path) in joined


# --------------------------------------------------------------------------- #
# Classification                                                              #
# --------------------------------------------------------------------------- #


def _result(
    *,
    cli: str,
    exit_code: int = 0,
    stderr: str = "",
    stdout: str = "ok",
    timed_out: bool = False,
    executable_not_found: bool = False,
) -> AdapterResult:
    return AdapterResult(
        cli=cli,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        command=[cli],
        timed_out=timed_out,
        executable_not_found=executable_not_found,
    )


def test_classify_success() -> None:
    classification = classify_result(_result(cli="codex"))
    assert classification.kind is DispatchErrorKind.SUCCESS


def test_classify_timeout() -> None:
    classification = classify_result(_result(cli="codex", timed_out=True))
    assert classification.kind is DispatchErrorKind.TIMEOUT


def test_classify_executable_not_found() -> None:
    classification = classify_result(
        _result(
            cli="codex",
            exit_code=-2,
            stderr="executable not found: codex",
            executable_not_found=True,
        )
    )
    assert classification.kind is DispatchErrorKind.EXECUTABLE_NOT_FOUND


def test_classify_auth_required_429() -> None:
    classification = classify_result(
        _result(cli="codex", exit_code=1, stderr="HTTP 401 Unauthorized")
    )
    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED
    assert (
        "codex login" in classification.suggested_action.lower() or classification.suggested_action
    )


def test_classify_rate_limited() -> None:
    classification = classify_result(
        _result(cli="codex", exit_code=1, stderr="Error: 429 Too Many Requests")
    )
    assert classification.kind is DispatchErrorKind.RATE_LIMITED


def test_classify_overloaded() -> None:
    classification = classify_result(
        _result(cli="codex", exit_code=1, stderr="503 Service Unavailable")
    )
    assert classification.kind is DispatchErrorKind.OVERLOADED


def test_classify_refused() -> None:
    classification = classify_result(
        _result(cli="claude", exit_code=0, stdout="I can't help with that.")
    )
    assert classification.kind is DispatchErrorKind.REFUSED


def test_classify_no_output() -> None:
    classification = classify_result(_result(cli="codex", exit_code=0, stdout="", stderr=""))
    assert classification.kind is DispatchErrorKind.NO_OUTPUT


def test_classify_exit_nonzero_falls_through() -> None:
    classification = classify_result(
        _result(cli="codex", exit_code=42, stderr="some unrelated failure")
    )
    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


# --------------------------------------------------------------------------- #
# render_prompt                                                               #
# --------------------------------------------------------------------------- #


def _make_task(
    tmp_path: Path, *, template_text: str, extra: dict[str, str] | None = None
) -> TaskSpec:
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "prompt.md"
    template.write_text(template_text, encoding="utf-8")
    return TaskSpec(
        id="T-X",
        title="Smoke test",
        target_repo=repo,
        cli="codex",
        prompt_template=template,
        worktree_branch="codex/x",
        allowed_paths=["src/**"],
        forbidden_paths=["secrets/*"],
        context_files=[],
        acceptance=["pytest"],
        timeout_seconds=600,
        base_ref="main",
        model="gpt-5.5",
        reasoning_effort="xhigh",
        extra_prompt_vars=dict(extra or {}),
        runs_dir=None,
        spec_path=tmp_path / "task.json",
        resolved_via="explicit",
    )


def test_render_prompt_substitutes_standard_vars(tmp_path: Path) -> None:
    template = textwrap.dedent(
        """
        ID: {{task_id}}
        Branch: {{worktree_branch}}
        Allowed:
        {{allowed_paths_block}}
        Forbidden:
        {{forbidden_paths_block}}
        Acceptance:
        {{acceptance_block}}
        """
    ).strip()
    task = _make_task(tmp_path, template_text=template)
    rendered = render_prompt(task)

    assert "ID: T-X" in rendered
    assert "Branch: codex/x" in rendered
    assert "- src/**" in rendered
    assert "- secrets/*" in rendered
    assert "- `pytest`" in rendered
    assert "{{" not in rendered


def test_render_prompt_refuses_unfilled_placeholder(tmp_path: Path) -> None:
    template = "Hello {{nonexistent_var}} world"
    task = _make_task(tmp_path, template_text=template)
    with pytest.raises(ValueError, match="unfilled placeholders"):
        render_prompt(task)


def test_render_prompt_extra_vars_supply_template_placeholders(tmp_path: Path) -> None:
    template = "Summary: {{task_summary}}"
    task = _make_task(
        tmp_path,
        template_text=template,
        extra={"task_summary": "do the thing"},
    )
    assert render_prompt(task) == "Summary: do the thing"


# --------------------------------------------------------------------------- #
# Task spec loading                                                           #
# --------------------------------------------------------------------------- #


def test_load_task_resolves_model_registry(tmp_path: Path) -> None:
    from atlas_dispatch import load_task

    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "prompt.md"
    template.write_text("hi", encoding="utf-8")

    spec = {
        "id": "T-1",
        "title": "test",
        "target_repo": str(repo),
        "model": "codex/gpt-6-sol-high",
        "prompt_template": str(template),
        "worktree_branch": "codex/t-1",
        "allowed_paths": ["**"],
    }
    spec_path = tmp_path / "task.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    task = load_task(spec_path)
    assert task.cli == "codex"
    assert task.model == "gpt-6-sol"
    assert task.reasoning_effort == "high"
    assert task.resolved_via == "model_registry:codex/gpt-6-sol-high"


def test_fake_cli_is_registered_and_needs_no_path_lookup() -> None:
    import sys

    from atlas_dispatch.adapter import CLIS, MODELS

    fake = CLIS["fake"]
    assert fake.argv_template == [sys.executable, "-m", "atlas_dispatch.fake_agent"]
    assert MODELS["fake/scripted"].cli == "fake"
