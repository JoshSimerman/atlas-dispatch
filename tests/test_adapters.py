"""Tests for the public adapter protocol surface."""

from __future__ import annotations

import inspect
import os
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from atlas_dispatch import (
    Adapter,
    AdapterResult,
    DispatchErrorKind,
    SubprocessAdapter,
    inject_mcp_config,
    render_command,
    run_cli,
)


def _keyword_only_signature_items(
    target: Callable[..., object],
) -> list[tuple[str, inspect._ParameterKind, object]]:
    return [
        (name, parameter.kind, parameter.default)
        for name, parameter in inspect.signature(target).parameters.items()
        if parameter.kind == inspect.Parameter.KEYWORD_ONLY
    ]


class _InMemoryAdapter:
    def __init__(self, result: AdapterResult) -> None:
        self.result = result

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int = 1800,
        extra_env: dict[str, str] | None = None,
    ) -> AdapterResult:
        return self.result


def test_subprocess_adapter_is_runtime_adapter() -> None:
    assert isinstance(SubprocessAdapter(cli="codex"), Adapter)


def test_subprocess_adapter_run_signature_matches_protocol() -> None:
    assert _keyword_only_signature_items(
        SubprocessAdapter.run
    ) == _keyword_only_signature_items(Adapter.run)


def test_in_memory_adapter_structurally_matches_protocol(tmp_path: Path) -> None:
    expected = AdapterResult(
        cli="memory",
        exit_code=0,
        stdout="done",
        stderr="",
        duration_seconds=0.0,
        command=["memory"],
    )
    adapter = _InMemoryAdapter(expected)

    assert isinstance(adapter, Adapter)
    assert (
        adapter.run(
            prompt="prompt",
            cwd=tmp_path,
            model="test-model",
            reasoning_effort="low",
            timeout_seconds=1,
            extra_env={"EXAMPLE": "1"},
        )
        is expected
    )


def test_subprocess_adapter_missing_executable_classifies(
    tmp_path: Path,
) -> None:
    env_var = "ATLAS_DISPATCH_CODEX_CMD"
    previous = os.environ.get(env_var)
    missing_executable = tmp_path / "atlas_dispatch_definitely_missing_executable.exe"
    os.environ[env_var] = shlex.quote(missing_executable.as_posix())

    try:
        result = SubprocessAdapter(cli="codex").run(
            prompt="hello",
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="xhigh",
            timeout_seconds=1,
        )
    finally:
        if previous is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = previous

    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.EXECUTABLE_NOT_FOUND


def test_subprocess_adapter_runs_in_provided_cwd(tmp_path: Path) -> None:
    """Regression: SubprocessAdapter.run must pass cwd= to subprocess.run.

    Without it, CLIs that lack a native cwd flag (e.g. gemini in v0.1.x)
    inherit atlas-dispatch's parent cwd instead of running inside the
    task's worktree, silently producing zero changes.
    """
    import sys

    env_var = "ATLAS_DISPATCH_CODEX_CMD"
    previous = os.environ.get(env_var)
    os.environ[env_var] = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote("import os, sys; sys.stdout.write(os.getcwd())")
    )

    try:
        result = SubprocessAdapter(cli="codex").run(
            prompt="",
            cwd=tmp_path,
            timeout_seconds=10,
        )
    finally:
        if previous is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = previous

    assert result.exit_code == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_render_command_substitutes_prompt_for_hermes(tmp_path: Path) -> None:
    prompt = "print exactly OK"

    command = render_command(
        cli="hermes",
        cwd=tmp_path,
        model="gpt-5.5",
        reasoning_effort="high",
        prompt=prompt,
    )

    assert command[-1] == prompt


def test_inject_mcp_config_codex_returns_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / ".atlas-dispatch-mcp.json"
    command = ["codex", "exec", "-"]

    injected = inject_mcp_config("codex", command, config_path)

    assert injected == command
    assert command == ["codex", "exec", "-"]


def test_inject_mcp_config_gemini_returns_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / ".atlas-dispatch-mcp.json"
    command = ["gemini", "--prompt", ""]

    injected = inject_mcp_config("gemini", command, config_path)

    assert injected == command
    assert command == ["gemini", "--prompt", ""]


def test_run_cli_for_hermes_passes_prompt_via_argv_not_stdin(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_subprocess_command(**kwargs: Any) -> AdapterResult:
        calls.append(kwargs)
        return AdapterResult(
            cli=kwargs["cli"],
            exit_code=0,
            stdout="done",
            stderr="",
            duration_seconds=0.0,
            command=kwargs["command"],
        )

    prompt = "print exactly OK"
    with (
        patch("atlas_dispatch.adapter._run_subprocess_command", fake_subprocess_command),
        patch("atlas_dispatch.adapter._run_codex_client", fake_subprocess_command),
    ):
        hermes_result = run_cli(
            cli="hermes",
            prompt=prompt,
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="high",
            timeout_seconds=10,
        )
        codex_result = run_cli(
            cli="codex",
            prompt=prompt,
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="high",
            timeout_seconds=10,
        )

    assert hermes_result.exit_code == 0
    assert codex_result.exit_code == 0
    assert calls[0]["command"][-1] == prompt
    assert calls[0]["prompt"] == ""
    assert prompt not in calls[1]["command"]
    assert calls[1]["prompt"] == prompt


class _RecordingStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FileBackedFakeProcess:
    def __init__(
        self,
        stdout_text: str = "done",
        stderr_text: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdin = _RecordingStdin()
        self.returncode = returncode
        self.stdout_text = stdout_text
        self.stderr_text = stderr_text

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def test_run_cli_strips_benign_macos_malloc_stack_logging_warning(
    tmp_path: Path,
) -> None:
    def fake_popen(command: list[str], **kwargs: object) -> _FileBackedFakeProcess:
        del command
        process = _FileBackedFakeProcess(
            stdout_text=(
                "codex(45266) MallocStackLogging: can't turn off malloc stack "
                "logging because it was not enabled.\nreal stdout\n"
            ),
            stderr_text=(
                "node(123) MallocStackLogging: can't turn off malloc stack "
                "logging because it was not enabled.\nreal stderr\n"
            ),
            returncode=1,
        )
        kwargs["stdout"].write(process.stdout_text)  # type: ignore[index, union-attr]
        kwargs["stderr"].write(process.stderr_text)  # type: ignore[index, union-attr]
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="codex",
            prompt="prompt",
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="high",
            timeout_seconds=10,
            command=["codex", "-"],
        )

    assert "MallocStackLogging" not in result.stdout
    assert "MallocStackLogging" not in result.stderr
    assert result.stdout == "real stdout\n"
    assert result.stderr == "real stderr\n"


def test_run_cli_existing_clis_still_use_stdin(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    processes: list[_FileBackedFakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FileBackedFakeProcess:
        calls.append({"command": command, **kwargs})
        process = _FileBackedFakeProcess(returncode=0)
        processes.append(process)
        kwargs["stdout"].write(process.stdout_text)  # type: ignore[index, union-attr]
        kwargs["stderr"].write(process.stderr_text)  # type: ignore[index, union-attr]
        return process

    prompt = "unique stdin prompt"
    # `gemini` is deliberately NOT in this cohort any more: it now runs the
    # Antigravity binary (agy), which takes the prompt as a `-p` ARGUMENT.
    # Rather than let it silently drop out of the guard, its opposite shape is
    # asserted directly in test_run_cli_agy_gemini_passes_prompt_as_argv below.
    cli_models = {
        "codex": ("gpt-5.5", "high"),
        "claude": ("claude-sonnet-4-6", None),
        "kimi": ("kimi-code/kimi-for-coding", "--thinking"),
    }

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        for cli, (model, reasoning_effort) in cli_models.items():
            result = run_cli(
                cli=cli,
                prompt=prompt,
                cwd=tmp_path,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout_seconds=10,
                command=[cli, "-"],
            )
            assert result.exit_code == 0

    assert len(calls) == len(cli_models)
    for call, process in zip(calls, processes, strict=True):
        assert process.stdin.writes == [prompt]
        assert prompt not in call["command"]


def test_run_cli_agy_gemini_passes_prompt_as_argv_not_stdin(tmp_path: Path) -> None:
    """gemini (agy) is the inverse of the stdin cohort: prompt in argv, stdin empty.

    Sending it BOTH ways is not harmless -- agy exits 2 with its usage text in
    0.04s, which classifies as a generic non-zero exit rather than as
    'the prompt never arrived'. Assert the shape so the regression is loud.
    """
    calls: list[dict[str, Any]] = []
    processes: list[_FileBackedFakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FileBackedFakeProcess:
        calls.append({"command": command, **kwargs})
        process = _FileBackedFakeProcess(returncode=0)
        processes.append(process)
        kwargs["stdout"].write(process.stdout_text)  # type: ignore[index, union-attr]
        kwargs["stderr"].write(process.stderr_text)  # type: ignore[index, union-attr]
        return process

    prompt = "unique argv prompt"
    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="gemini",
            prompt=prompt,
            cwd=tmp_path,
            model="gemini-3.6-flash",
            reasoning_effort=None,
            timeout_seconds=10,
            command=["agy", "--dangerously-skip-permissions", "-p", prompt],
        )

    assert result.exit_code == 0
    assert prompt in calls[0]["command"]
    # stdin is still opened and closed (a single empty write); what must never
    # happen is the PROMPT going down it as well as into argv.
    assert "".join(processes[0].stdin.writes) == ""
