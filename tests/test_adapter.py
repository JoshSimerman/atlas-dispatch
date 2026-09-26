"""Focused adapter regression tests for Kimi Wire fixups."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from atlas_dispatch.adapter import (
    BEAT_INTERVAL_SECONDS,
    CLI_COMMON_ENV_ALLOWLIST,
    CLI_ISOLATION_ENV,
    CLIS,
    KIMI_WIRE_PROTOCOL_VERSION,
    MODELS,
    UNKNOWN_EFFECTIVE_MODEL,
    AdapterResult,
    Classification,
    DispatchErrorKind,
    QuotaResetWindowProvenance,
    classify_result,
    render_command,
    resolve_invocation,
    run_cli,
)
from atlas_dispatch.dispatcher import (
    TaskSpec,
    _expected_review_deliverable_path,
    render_prompt,
)
from atlas_dispatch.worktree import worktree_path_for


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_explicit_scope_refusal_is_distinct_and_carries_builder_reason() -> None:
    reason = (
        "I cannot implement the requested fix because the required config file "
        "is outside the allowed_paths for this task."
    )

    classified = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=0,
            stdout=reason,
            stderr="",
            duration_seconds=1.0,
            command=["codex"],
            final_turn_status="completed",
        )
    )

    assert classified.kind is DispatchErrorKind.REFUSED_OUT_OF_SCOPE
    assert classified.kind is not DispatchErrorKind.NO_CHANGE
    assert reason in classified.suggested_action


def test_scope_discussion_without_explicit_refusal_remains_success() -> None:
    classified = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=0,
            stdout=(
                "I reviewed the allowed_paths and implemented the change entirely "
                "inside the allowed scope."
            ),
            stderr="",
            duration_seconds=1.0,
            command=["codex"],
            final_turn_status="completed",
        )
    )

    assert classified.kind is DispatchErrorKind.SUCCESS


def test_successful_completion_with_scope_compliance_promise_remains_success() -> None:
    classified = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=0,
            stdout=(
                "Implemented the requested change and all tests pass. "
                "I will not modify files outside the allowed scope."
            ),
            stderr="",
            duration_seconds=1.0,
            command=["codex"],
            final_turn_status="completed",
        )
    )

    assert classified.kind is DispatchErrorKind.SUCCESS


def test_required_work_blocked_by_boundary_is_a_scope_refusal() -> None:
    reason = (
        "I cannot complete the required work without modifying settings_service.py, "
        "which is outside the allowed paths."
    )
    classified = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=0,
            stdout=reason,
            stderr="",
            duration_seconds=1.0,
            command=["codex"],
            final_turn_status="completed",
        )
    )

    assert classified.kind is DispatchErrorKind.REFUSED_OUT_OF_SCOPE
    assert reason in classified.suggested_action


class _BlockingStream:
    def __init__(self, process: _FakeProcess) -> None:
        self.process = process

    def __iter__(self) -> _BlockingStream:
        return self

    def __next__(self) -> str:
        while self.process.returncode is None:
            time.sleep(0.001)
        raise StopIteration


class _YieldThenBlockStream:
    def __init__(self, process: _FakeProcess, lines: list[str]) -> None:
        self.process = process
        self.lines = list(lines)

    def __iter__(self) -> _YieldThenBlockStream:
        return self

    def __next__(self) -> str:
        if self.lines:
            return self.lines.pop(0)
        while self.process.returncode is None:
            time.sleep(0.001)
        raise StopIteration


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str] | None = None,
        stderr_lines: list[str] | None = None,
        *,
        block: bool = False,
        returncode: int = 0,
        wait_timeout_once: bool = False,
        wait_timeouts: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self.returncode: int | None = None
        self.final_returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_timeouts = wait_timeouts + (1 if wait_timeout_once else 0)
        self.wait_timeouts_seen: list[float | None] = []
        self.stdout = _BlockingStream(self) if block else iter(stdout_lines or [])
        self.stderr = iter(stderr_lines or [])

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts_seen.append(timeout)
        if self.wait_timeouts > 0:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="kimi", timeout=timeout)
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _wire_line(payload: dict[str, object]) -> str:
    return json.dumps(payload) + "\n"


def test_codex_exec_json_command_parses_turn_lifecycle(tmp_path: Path) -> None:
    process = _FakeProcess(
        [
            _wire_line({"type": "thread.started", "thread_id": "thread-1"}),
            _wire_line({"type": "turn.started"}),
            _wire_line(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_0",
                        "type": "agent_message",
                        "text": "done",
                    },
                }
            ),
            _wire_line(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                }
            ),
        ]
    )
    captured: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="codex",
            prompt="prompt",
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="high",
            timeout_seconds=10,
        )

    assert "--json" in captured["command"]
    assert result.exit_code == 0
    assert result.stdout == "done"
    assert result.final_turn_status == "completed"
    assert result.token_usage == {"input": 3, "output": 4, "total": 7}
    assert result.turn_lifecycle is not None
    assert [event["event"] for event in result.turn_lifecycle] == [
        "thread.started",
        "turn.started",
        "item.completed",
        "turn.completed",
    ]
    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.SUCCESS


def test_codex_runtime_mode_defaults_to_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_RUNTIME_MODE", raising=False)
    process = _FakeProcess([
        _wire_line({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        _wire_line({"type": "turn.completed"}),
    ])
    calls: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del kwargs
        calls.append(command)
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(cli="codex", prompt="prompt", cwd=tmp_path, timeout_seconds=10)

    assert result.exit_code == 0
    assert result.stdout == "done"
    assert len(calls) == 1
    assert "exec" in calls[0]
    assert "--json" in calls[0]


def test_codex_runtime_mode_subprocess_keeps_p1_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_RUNTIME_MODE", "subprocess")
    process = _FakeProcess([
        _wire_line({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        _wire_line({"type": "turn.completed"}),
    ])
    calls: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del kwargs
        calls.append(command)
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(cli="codex", prompt="prompt", cwd=tmp_path, timeout_seconds=10)

    assert result.exit_code == 0
    assert result.stdout == "done"
    assert len(calls) == 1


def test_codex_runtime_mode_app_server_is_explicit_phase_a_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_RUNTIME_MODE", "app_server")

    with patch("atlas_dispatch.adapter.subprocess.Popen") as popen:
        result = run_cli(cli="codex", prompt="prompt", cwd=tmp_path, timeout_seconds=10)

    popen.assert_not_called()
    assert result.exit_code == 1
    assert result.command == ["codex", "app-server"]
    assert "CODEX_RUNTIME_MODE=app_server" in result.stderr
    assert "Phase B" in result.stderr
    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_codex_runtime_mode_invalid_value_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_RUNTIME_MODE", "websocket")

    with patch("atlas_dispatch.adapter.subprocess.Popen") as popen:
        result = run_cli(cli="codex", prompt="prompt", cwd=tmp_path, timeout_seconds=10)

    popen.assert_not_called()
    assert result.exit_code == 1
    assert "invalid CODEX_RUNTIME_MODE='websocket'" in result.stderr
    assert "subprocess" in result.stderr
    assert "app_server" in result.stderr
    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_codex_runtime_mode_is_ignored_for_non_codex_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_RUNTIME_MODE", "app_server")
    process = _FakeProcess(returncode=0)
    calls: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del kwargs
        calls.append(command)
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="gemini",
            prompt="prompt",
            cwd=tmp_path,
            command=["gemini"],
            timeout_seconds=10,
        )

    assert result.exit_code == 0
    assert calls == [["gemini"]]


def test_codex_exec_json_idle_stall_terminates_process(tmp_path: Path) -> None:
    process = _FakeProcess(block=True)
    process.stdout = _YieldThenBlockStream(
        process,
        [_wire_line({"type": "turn.started"})],
    )

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="codex",
            prompt="prompt",
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="high",
            timeout_seconds=10,
            idle_timeout_seconds=0,
        )

    assert process.terminated
    assert result.timed_out
    assert result.idle_classification == "stalled"
    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.STALLED


def test_codex_exec_json_no_event_idle_stall_terminates_process(tmp_path: Path) -> None:
    process = _FakeProcess(block=True)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="codex",
            prompt="prompt",
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="high",
            timeout_seconds=1,
            idle_timeout_seconds=0,
        )

    assert process.terminated
    assert result.timed_out
    assert result.idle_classification == "stalled"
    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.STALLED


def test_codex_exec_json_approval_required_terminates_process(tmp_path: Path) -> None:
    process = _FakeProcess(block=True)
    process.stdout = _YieldThenBlockStream(
        process,
        [
            _wire_line({"type": "turn.started"}),
            _wire_line({"type": "approval_required"}),
        ],
    )

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="codex",
            prompt="prompt",
            cwd=tmp_path,
            model="gpt-5.5",
            reasoning_effort="high",
            timeout_seconds=10,
            idle_timeout_seconds=60,
        )

    assert process.terminated
    assert not result.timed_out
    assert result.idle_classification == "approval_blocked"
    assert "approval-required" in result.stderr
    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.APPROVAL_BLOCKED


def _wire_init_response() -> str:
    return _wire_line(
        {"jsonrpc": "2.0", "id": "1", "result": {"protocol_version": "1.10"}}
    )


def _wire_prompt_finished() -> str:
    return _wire_line(
        {"jsonrpc": "2.0", "id": "2", "result": {"status": "finished"}}
    )


def _run_wire_with_lines(tmp_path: Path, lines: list[str]) -> tuple[Any, _FakeProcess]:
    process = _FakeProcess(lines)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi-streaming",
            prompt="prompt",
            cwd=tmp_path,
            model="kimi-code/kimi-for-coding",
            reasoning_effort="--thinking",
            timeout_seconds=10,
        )
    return result, process


def test_kimi_wire_protocol_version_is_current() -> None:
    assert KIMI_WIRE_PROTOCOL_VERSION == "1.10"


def test_kimi_session_id_includes_full_path_hash(tmp_path: Path) -> None:
    first = tmp_path / "one" / "same-leaf"
    second = tmp_path / "two" / "same-leaf"

    first_command = render_command(cli="kimi-streaming", cwd=first)
    second_command = render_command(cli="kimi-streaming", cwd=second)

    first_session = first_command[first_command.index("--session") + 1]
    second_session = second_command[second_command.index("--session") + 1]
    assert first_session.startswith("atlas-dispatch-same-leaf-")
    assert second_session.startswith("atlas-dispatch-same-leaf-")
    assert first_session != second_session


def test_empty_reasoning_effort_token_is_filtered(tmp_path: Path) -> None:
    command = render_command(
        cli="kimi",
        cwd=tmp_path,
        model="kimi-code/kimi-for-coding",
        reasoning_effort=None,
    )

    assert "" not in command
    assert command[command.index("--model") + 2] == "--work-dir"


def test_kimi_code_command_passes_prompt_as_argument(tmp_path: Path) -> None:
    model = MODELS["kimi/k2.7-coding"]
    prompt = "Implement the task in this worktree."

    command = render_command(
        cli=model.cli,
        cwd=tmp_path,
        model=model.model_id,
        reasoning_effort=model.reasoning_effort,
        prompt=prompt,
    )

    assert command[0].endswith("kimi")
    assert command[command.index("-p") + 1] == prompt
    assert command[command.index("-m") + 1] == "kimi-code/kimi-for-coding"
    assert command[command.index("--output-format") + 1] == "text"
    # `-p`/--prompt is already non-interactive and auto-executes tools in
    # 0.6.0; the interactive approval flags conflict with it (the CLI
    # hard-errors "Cannot combine --prompt with --yolo/--auto"), so they
    # must be absent.
    assert "-y" not in command
    assert "--yolo" not in command
    assert "--auto" not in command
    assert "--print" not in command
    assert "--input-format" not in command
    assert "--final-message-only" not in command
    assert CLIS["kimi-code"].reads_prompt_from_stdin is False


def test_kimi_code_env_override_uses_underscore_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ATLAS_DISPATCH_KIMI_CODE_CMD",
        "custom-kimi -p {prompt} -m {model}",
    )

    command = render_command(
        cli="kimi-code",
        cwd=tmp_path,
        model="kimi-code/kimi-for-coding",
        prompt="hello",
    )

    assert command == [
        "custom-kimi",
        "-p",
        "hello",
        "-m",
        "kimi-code/kimi-for-coding",
    ]


def test_kimi_models_do_not_route_to_retired_print_argv() -> None:
    kimi_models = {
        name: model for name, model in MODELS.items() if name.startswith("kimi/")
    }

    assert kimi_models
    assert "kimi/k2.6-high-streaming" not in kimi_models
    for model in kimi_models.values():
        definition = CLIS[model.cli]
        assert "--print" not in definition.argv_template
        assert "--input-format" not in definition.argv_template
        assert "--final-message-only" not in definition.argv_template


def test_model_registry_names_match_keys() -> None:
    for name, model in MODELS.items():
        assert model.name == name


def test_codex_gpt6_astra_and_terra_models_registered() -> None:
    expected = {
        "codex/gpt-6-astra-medium": ("codex", "gpt-6-astra", "medium"),
        "codex/gpt-6-astra-high": ("codex", "gpt-6-astra", "high"),
        "codex/gpt-5.6-terra-medium": ("codex", "gpt-5.6-terra", "medium"),
    }

    for name, (cli, model_id, reasoning_effort) in expected.items():
        model = MODELS[name]
        assert model.cli == cli
        assert model.model_id == model_id
        assert model.reasoning_effort == reasoning_effort


def test_codex_gpt6_sol_luna_models_registered() -> None:
    expected = {
        "codex/gpt-6-sol-medium": ("codex", "gpt-6-sol", "medium"),
        "codex/gpt-6-sol-high": ("codex", "gpt-6-sol", "high"),
        "codex/gpt-6-sol-xhigh": ("codex", "gpt-6-sol", "xhigh"),
        "codex/gpt-6-luna-low": ("codex", "gpt-6-luna", "low"),
        "codex/gpt-6-luna-medium": ("codex", "gpt-6-luna", "medium"),
    }

    for name, (cli, model_id, reasoning_effort) in expected.items():
        model = MODELS[name]
        assert model.cli == cli
        assert model.model_id == model_id
        assert model.reasoning_effort == reasoning_effort


def test_codex_gpt6_sol_medium_invocation_uses_model_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_DISPATCH_CODEX_CMD", raising=False)
    model = MODELS["codex/gpt-6-sol-medium"]

    invocation = resolve_invocation(
        cli=model.cli,
        cwd=tmp_path,
        model=model.model_id,
        reasoning_effort=model.reasoning_effort,
        requested_model=model.name,
        resolved_via=f"model_registry:{model.name}",
        prompt="Review the assigned change.",
    )

    assert invocation.command[invocation.command.index("-m") + 1] == "gpt-6-sol"


def test_kimi_k27_and_k3_names_map_to_kimi_code_aliases() -> None:
    expected = {
        "kimi/k2.7-coding": "kimi-code/kimi-for-coding",
        "kimi/k2.7-coding-highspeed": "kimi-code/kimi-for-coding-highspeed",
        "kimi/k3-max": "kimi-code/k3",
        "kimi/k3-256k": "kimi-code/k3-256k",
    }
    for name, alias in expected.items():
        assert MODELS[name].cli == "kimi-code"
        assert MODELS[name].model_id == alias


def test_kimi_models_use_node_kimi_code_shape(tmp_path: Path) -> None:
    for name in ("kimi/k2.7-coding", "kimi/k2.7-coding-highspeed", "kimi/k3-max"):
        model = MODELS[name]
        command = render_command(
            cli=model.cli,
            cwd=tmp_path,
            model=model.model_id,
            reasoning_effort=model.reasoning_effort,
            prompt="Implement the task.",
        )

        assert model.cli == "kimi-code"
        assert model.reasoning_effort is None
        assert command[command.index("-p") + 1] == "Implement the task."
        assert command[command.index("-m") + 1] == model.model_id
        assert "--print" not in command
        assert "--thinking" not in command
        assert "--no-thinking" not in command


def test_command_override_filters_empty_tokens_before_exec(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    process = _FakeProcess()

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del kwargs
        calls.append(command)
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi",
            prompt="prompt",
            cwd=tmp_path,
            model="model",
            reasoning_effort=None,
            command=["kimi", "{reasoning_effort}", "--work-dir", "{cwd}"],
        )

    assert result.exit_code == 0
    assert calls == [["kimi", "--work-dir", str(tmp_path)]]


def test_subprocess_command_beats_heartbeat_during_long_wait(tmp_path: Path) -> None:
    heartbeat = Mock()
    process = _FakeProcess(wait_timeouts=2)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi",
            prompt="prompt",
            cwd=tmp_path,
            model="model",
            timeout_seconds=90,
            command=["kimi"],
            heartbeat=heartbeat,
        )

    assert result.exit_code == 0
    assert heartbeat.beat.call_count >= 2
    assert process.wait_timeouts_seen[:2] == [
        BEAT_INTERVAL_SECONDS,
        BEAT_INTERVAL_SECONDS,
    ]


def test_subprocess_command_works_with_none_heartbeat(tmp_path: Path) -> None:
    process = _FakeProcess(returncode=0)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi",
            prompt="prompt",
            cwd=tmp_path,
            model="model",
            timeout_seconds=10,
            command=["kimi"],
            heartbeat=None,
        )

    assert result.exit_code == 0


def test_cli_subprocess_scrubs_poisoned_ambient_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison_bin = tmp_path / "poison-bin"
    poison_bin.mkdir()
    monkeypatch.setenv("PATH", f"{poison_bin}{os.pathsep}/usr/bin")
    monkeypatch.setenv("PYTHONPATH", "/poison/python")
    monkeypatch.setenv("PYTHONHOME", "/poison/home")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.status")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!echo poisoned")
    monkeypatch.setenv("ATLAS_DISPATCH_FAKE_CMD", "secret override")
    monkeypatch.setenv("SECRET_TOKEN", "do-not-forward")
    monkeypatch.setenv("KIMI_API_KEY", "required-provider-token")
    monkeypatch.setenv("UV_CACHE_DIR", "/poison/uv-cache")
    uv_cache = tmp_path / "run" / "uv-cache"

    result = run_cli(
        cli="kimi",
        prompt="",
        cwd=tmp_path,
        command=["/usr/bin/env"],
        extra_env={"UV_CACHE_DIR": str(uv_cache)},
        timeout_seconds=10,
    )

    assert result.exit_code == 0, result.stderr
    child_env = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    allowed_keys = (
        set(CLI_COMMON_ENV_ALLOWLIST)
        | set(CLIS["kimi"].env_allowlist)
        | set(CLI_ISOLATION_ENV)
        | {"PATH", "UV_CACHE_DIR"}
    )
    assert set(child_env) <= allowed_keys
    assert str(poison_bin) not in child_env["PATH"]
    assert "PYTHONPATH" not in child_env
    assert "PYTHONHOME" not in child_env
    assert "GIT_CONFIG_COUNT" not in child_env
    assert "GIT_CONFIG_KEY_0" not in child_env
    assert "GIT_CONFIG_VALUE_0" not in child_env
    assert "ATLAS_DISPATCH_FAKE_CMD" not in child_env
    assert "SECRET_TOKEN" not in child_env
    assert child_env["KIMI_API_KEY"] == "<redacted-env:KIMI_API_KEY>"
    assert child_env["UV_CACHE_DIR"] == str(uv_cache)
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert child_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert child_env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "do-not-forward" not in result.stdout
    assert "required-provider-token" not in result.stdout

    assert os.environ["SECRET_TOKEN"] == "do-not-forward"
    assert os.environ["UV_CACHE_DIR"] == "/poison/uv-cache"
    identity = result.run_identity
    assert identity is not None
    assert identity["policy_version"] == "1"
    assert "KIMI_API_KEY" in identity["forwarded_cli_env_keys"]
    assert identity["uv_cache_path"] == str(uv_cache)
    assert identity["executable"]["resolved_path"] == "/usr/bin/env"
    assert len(identity["executable"]["sha256"]) == 64
    identity_text = json.dumps(identity)
    assert "required-provider-token" not in identity_text
    assert "do-not-forward" not in identity_text


@pytest.mark.parametrize(
    "command",
    [None, ["kimi"]],
    ids=["default-registry-rendering", "bare-command"],
)
def test_cli_executable_resolution_ignores_poisoned_ambient_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str] | None,
) -> None:
    trusted_home = tmp_path / "trusted-home"
    trusted_bin = trusted_home / ".local" / "bin"
    trusted_bin.mkdir(parents=True)
    trusted_executable = trusted_bin / "kimi"
    trusted_executable.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$PATH\"\n",
        encoding="utf-8",
    )
    trusted_executable.chmod(0o755)

    poison_bin = tmp_path / "poison-bin"
    poison_bin.mkdir()
    poison_executable = poison_bin / "kimi"
    poison_executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    poison_executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(trusted_home))
    monkeypatch.setenv("PATH", f"{poison_bin}{os.pathsep}/usr/bin")

    result = run_cli(
        cli="kimi-code",
        prompt="prompt",
        cwd=tmp_path,
        model="kimi-code/kimi-for-coding",
        command=command,
        timeout_seconds=10,
    )

    assert result.exit_code == 0, result.stderr
    expected_command = str(trusted_executable) if command is None else "kimi"
    assert result.command[0] == expected_command
    child_path = result.stdout.strip().split(os.pathsep)
    assert child_path[0] == str(trusted_bin)
    assert str(poison_bin) not in child_path
    assert result.run_identity is not None
    assert result.run_identity["executable"]["resolved_path"] == str(
        trusted_executable.resolve()
    )
    assert str(poison_bin) not in json.dumps(result.run_identity)


@pytest.mark.parametrize(
    "cli",
    ["codex", "claude", "gemini", "kimi", "kimi-code", "hermes"],
)
def test_supported_cli_auth_and_config_environment_is_explicitly_forwarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: str,
) -> None:
    expected_keys = CLIS[cli].env_allowlist
    for key in expected_keys:
        monkeypatch.setenv(key, f"supported-{cli}-{key.lower()}")
    script = (
        "import json, os; "
        f"keys = {expected_keys!r}; "
        "print(json.dumps(sorted(key for key in keys if key in os.environ)))"
    )

    result = run_cli(
        cli=cli,
        prompt="",
        cwd=tmp_path,
        command=[sys.executable, "-c", script],
        timeout_seconds=10,
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == sorted(expected_keys)
    assert result.run_identity is not None
    assert result.run_identity["forwarded_cli_env_keys"] == sorted(expected_keys)
    identity_text = json.dumps(result.run_identity)
    assert f"supported-{cli}-" not in identity_text


def test_forwarded_cli_secret_is_redacted_from_adapter_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "echoed-secret-value")

    result = run_cli(
        cli="gemini",
        prompt="",
        cwd=tmp_path,
        command=[
            sys.executable,
            "-c",
            "import os; print(os.environ['GEMINI_API_KEY'])",
        ],
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "<redacted-env:GEMINI_API_KEY>"
    assert "echoed-secret-value" not in result.stdout
    assert "echoed-secret-value" not in result.stderr
    assert "echoed-secret-value" not in json.dumps(result.command)


def test_cli_subprocess_rejects_unallowlisted_extra_environment(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match=r"unsupported CLI environment override.*TOKEN",
    ):
        run_cli(
            cli="kimi",
            prompt="",
            cwd=tmp_path,
            command=[sys.executable, "-c", "pass"],
            extra_env={"UNSUPPORTED_TOKEN": "must-not-pass"},
        )


def test_codex_json_command_beats_heartbeat_per_event(tmp_path: Path) -> None:
    heartbeat = Mock()
    events = [
        _wire_line({"type": "thread.started", "thread_id": "thread-1"}),
        *[
            _wire_line(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"item_{index}",
                        "type": "agent_message",
                        "text": f"event {index}",
                    },
                }
            )
            for index in range(10)
        ],
        _wire_line({"type": "turn.completed"}),
    ]
    process = _FakeProcess(events)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="codex",
            prompt="prompt",
            cwd=tmp_path,
            model="gpt-5.5",
            timeout_seconds=10,
            command=["codex", "exec", "--json", "-"],
            heartbeat=heartbeat,
        )

    assert result.exit_code == 0
    assert heartbeat.beat.call_count >= len(events)


def test_kimi_auth_regex_requires_real_kimi_auth_signal() -> None:
    false_positive = classify_result(
        AdapterResult(
            cli="kimi",
            exit_code=1,
            stdout="",
            stderr="auth_expired appears in unrelated debug output",
            duration_seconds=0,
            command=["kimi"],
        )
    )
    real_error = classify_result(
        AdapterResult(
            cli="kimi",
            exit_code=1,
            stdout="",
            stderr="Error: auth_expired; please run the kimi login command\n",
            duration_seconds=0,
            command=["kimi"],
        )
    )

    assert false_positive.kind is DispatchErrorKind.EXIT_NONZERO
    assert real_error.kind is DispatchErrorKind.AUTH_REQUIRED


def test_codex_selected_model_at_capacity_classifies_overloaded() -> None:
    result = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=1,
            stdout="",
            stderr="Selected model is at capacity. Please try a different model.",
            duration_seconds=0,
            command=["codex"],
        )
    )

    assert result.kind is DispatchErrorKind.OVERLOADED
    assert result.matched_pattern is not None
    assert "capacity" in result.matched_pattern


def test_codex_usage_limit_still_classifies_rate_limited() -> None:
    result = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=1,
            stdout="",
            stderr=(
                "ERROR: You've hit your usage limit. Visit https://example.com "
                "or try again at 10:55 AM."
            ),
            duration_seconds=0,
            command=["codex"],
        )
    )

    assert result.kind is DispatchErrorKind.RATE_LIMITED
    assert result.matched_pattern is not None
    assert "usage\\s+limit" in result.matched_pattern


@pytest.mark.parametrize("exit_code", [1, 0])
def test_claude_session_limit_is_quota_exhausted_regardless_of_exit_code(
    exit_code: int,
) -> None:
    classification = classify_result(
        AdapterResult(
            cli="claude",
            exit_code=exit_code,
            stdout=(
                "You've hit your session limit · resets 4pm "
                "(America/Chicago)"
            ),
            stderr="",
            duration_seconds=0.2,
            command=["claude", "--print"],
        )
    )

    assert classification.kind is DispatchErrorKind.QUOTA_EXHAUSTED
    assert classification.quota_reset_window == "4pm (America/Chicago)"
    assert (
        classification.quota_reset_window_provenance
        is QuotaResetWindowProvenance.VENDOR_DECLARED
    )
    assert "wait for the quota reset" in classification.suggested_action.lower()


def test_successful_claude_response_quoting_session_limit_is_success() -> None:
    classification = classify_result(
        AdapterResult(
            cli="claude",
            exit_code=0,
            stdout=(
                "Implemented handling for the captured banner: "
                "You've hit your session limit · resets 4pm "
                "(America/Chicago). All tests pass."
            ),
            stderr="",
            duration_seconds=0.2,
            command=["claude", "--print"],
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS
    assert classification.quota_reset_window is None
    assert classification.quota_reset_window_provenance is None


def test_unrelated_claude_crash_remains_exit_nonzero() -> None:
    classification = classify_result(
        AdapterResult(
            cli="claude",
            exit_code=1,
            stdout="",
            stderr="Error: worker subprocess crashed while applying edits",
            duration_seconds=0.2,
            command=["claude", "--print"],
        )
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO
    assert classification.quota_reset_window is None
    assert classification.quota_reset_window_provenance is None


def test_capacity_in_prose_does_not_classify_overloaded() -> None:
    result = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=1,
            stdout="",
            stderr="the worker pool has spare capacity; assertion failed in test_foo",
            duration_seconds=0,
            command=["codex"],
        )
    )

    assert result.kind is DispatchErrorKind.EXIT_NONZERO


def test_agy_individual_quota_exhaustion_carries_vendor_reset_window() -> None:
    classification = classify_result(
        AdapterResult(
            cli="gemini",
            exit_code=1,
            stdout="",
            stderr=(
                "Error: Individual quota reached. Please upgrade your "
                "subscription to increase your limits. Resets in 162h15m13s.\n"
                "Error: timeout waiting for response\n"
            ),
            duration_seconds=8.53,
            command=["agy"],
            produced_expected_deliverable=True,
        )
    )

    assert classification.kind is DispatchErrorKind.QUOTA_EXHAUSTED
    assert classification.quota_reset_window == "162h15m13s"
    assert (
        classification.quota_reset_window_provenance
        is QuotaResetWindowProvenance.VENDOR_DECLARED
    )
    assert "vendor-declared reset window is 162h15m13s" in (
        classification.suggested_action.lower()
    )
    assert classification.observed_exit_code is None


def test_agy_quota_without_vendor_reset_does_not_invent_provenance() -> None:
    classification = classify_result(
        AdapterResult(
            cli="gemini",
            exit_code=1,
            stdout="",
            stderr=(
                "Error: Individual quota reached. Please upgrade your "
                "subscription to increase your limits.\n"
            ),
            duration_seconds=1,
            command=["agy"],
        )
    )

    assert classification.kind is DispatchErrorKind.QUOTA_EXHAUSTED
    assert classification.quota_reset_window is None
    assert classification.quota_reset_window_provenance is None
    assert "vendor-declared" not in classification.suggested_action.lower()


def test_quota_reset_window_and_provenance_cannot_be_recorded_separately() -> None:
    with pytest.raises(ValueError, match="must be recorded together"):
        Classification(
            kind=DispatchErrorKind.QUOTA_EXHAUSTED,
            suggested_action="Wait.",
            quota_reset_window_provenance=(
                QuotaResetWindowProvenance.VENDOR_DECLARED
            ),
        )


def _render_production_review_prompt(
    tmp_path: Path,
) -> tuple[TaskSpec, Path, str, str]:
    target_repo = tmp_path / "target"
    target_repo.mkdir()
    branch = "gemini/production-review-template-regression"
    review_path = "reviews/source-task-gemini-review.md"
    task = TaskSpec(
        id="REVIEW-PRODUCTION-TEMPLATE",
        title="Exercise the real review prompt",
        target_repo=target_repo,
        cli="gemini",
        prompt_template=(
            Path(__file__).resolve().parents[1] / "examples" / "prompts" / "review.md"
        ),
        worktree_branch=branch,
        allowed_paths=["reviews/**"],
        acceptance=[f"test -f {review_path}"],
        model="gemini-3.6-flash",
        reasoning_effort="high",
        extra_prompt_vars={
            "review_output_path": review_path,
            "source_task_id": "TASK-SOURCE",
            "source_agent": "codex-builder",
            "source_branch": "codex/source",
            "source_remote_ref": "origin/codex/source",
            "source_run_report": "",
            "source_run_summary": "Source run completed.",
            "source_changed_files": "- `atlas_dispatch/adapter.py`",
        },
    )
    workspace = worktree_path_for(target_repo, branch).resolve()
    workspace.mkdir()
    prompt = render_prompt(task)
    return task, workspace, review_path, prompt


def test_agy_post_completion_timeout_with_produced_review_is_success(
    tmp_path: Path,
) -> None:
    task, workspace, review_path, prompt = _render_production_review_prompt(tmp_path)
    expected_deliverable_path = _expected_review_deliverable_path(
        task,
        workspace=workspace,
    )
    assert expected_deliverable_path == workspace / review_path
    assert (
        "the harness will save it — it won't. After you exit, the dispatcher runs\n"
        f"`test -f {review_path}`"
    ) in prompt
    script = (
        "from pathlib import Path; "
        f"path = Path({review_path!r}); "
        "path.parent.mkdir(parents=True, exist_ok=True); "
        "path.write_text('# complete review\\n', encoding='utf-8'); "
        "raise SystemExit(7)"
    )

    result = run_cli(
        cli="gemini",
        prompt=prompt,
        cwd=workspace,
        expected_deliverable_path=expected_deliverable_path,
        command=[sys.executable, "-c", script],
        timeout_seconds=10,
    )

    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.EXIT_NONZERO

    # Supply agy's exact post-completion message through the real subprocess
    # result, rather than manually constructing completion evidence.
    script_with_signature = (
        "from pathlib import Path; import sys; "
        f"path = Path({review_path!r}); "
        "path.write_text('# complete review, revised\\n', encoding='utf-8'); "
        "sys.stderr.write('Error: timeout waiting for response\\n'); "
        "raise SystemExit(7)"
    )
    result = run_cli(
        cli="gemini",
        prompt=prompt,
        cwd=workspace,
        expected_deliverable_path=expected_deliverable_path,
        command=[sys.executable, "-c", script_with_signature],
        timeout_seconds=10,
    )

    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.SUCCESS
    assert result.classification.matched_pattern in (
        CLIS["gemini"].extra_post_completion_timeout_patterns
    )
    assert result.classification.observed_exit_code == 7
    assert result.exit_code == 0
    assert result.process_exit_code == 7
    assert result.produced_expected_deliverable is True
    assert result.expected_deliverable_path == str(workspace / review_path)
    assert result.run_identity is not None
    assert result.run_identity["process_exit_code"] == 7


def test_rendered_review_prompt_is_not_used_as_deliverable_evidence(
    tmp_path: Path,
) -> None:
    _task, workspace, review_path, prompt = _render_production_review_prompt(tmp_path)
    script = (
        "from pathlib import Path; import sys; "
        f"path = Path({review_path!r}); "
        "path.parent.mkdir(parents=True, exist_ok=True); "
        "path.write_text('# complete review\\n', encoding='utf-8'); "
        "sys.stderr.write('Error: timeout waiting for response\\n'); "
        "raise SystemExit(7)"
    )

    result = run_cli(
        cli="gemini",
        prompt=prompt,
        cwd=workspace,
        command=[sys.executable, "-c", script],
        timeout_seconds=10,
    )

    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.EXIT_NONZERO
    assert result.produced_expected_deliverable is False
    assert result.expected_deliverable_path is None


def test_agy_post_completion_timeout_without_review_stays_failure(
    tmp_path: Path,
) -> None:
    review_path = "reviews/missing-gemini-review.md"
    result = run_cli(
        cli="gemini",
        prompt="Prompt prose is not completion evidence.",
        cwd=tmp_path,
        expected_deliverable_path=tmp_path / review_path,
        command=[
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stderr.write('Error: timeout waiting for response\\n'); "
                "raise SystemExit(7)"
            ),
        ],
        timeout_seconds=10,
    )

    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.EXIT_NONZERO
    assert result.exit_code == 7
    assert result.process_exit_code is None
    assert result.produced_expected_deliverable is False
    assert not (tmp_path / review_path).exists()


@pytest.mark.parametrize(
    ("cli", "stderr"),
    [
        ("gemini", "Error: request timed out\n"),
        ("claude", "Error: timeout waiting for response\n"),
    ],
)
def test_post_completion_override_is_not_generic(
    cli: str,
    stderr: str,
) -> None:
    classification = classify_result(
        AdapterResult(
            cli=cli,
            exit_code=7,
            stdout="",
            stderr=stderr,
            duration_seconds=0,
            command=[cli],
            produced_expected_deliverable=True,
        )
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO
    assert classification.observed_exit_code is None


@pytest.mark.parametrize(
    ("message", "expected_kind"),
    [
        (
            "Waiting for authentication\nError: timeout waiting for response\n",
            DispatchErrorKind.AUTH_REQUIRED,
        ),
        (
            "RESOURCE_EXHAUSTED\nError: timeout waiting for response\n",
            DispatchErrorKind.RATE_LIMITED,
        ),
    ],
)
def test_agy_auth_and_rate_limit_classification_are_unchanged(
    tmp_path: Path,
    message: str,
    expected_kind: DispatchErrorKind,
) -> None:
    review_path = "reviews/missing-negative-control.md"
    result = run_cli(
        cli="gemini",
        prompt="Prompt prose is not completion evidence.",
        cwd=tmp_path,
        expected_deliverable_path=tmp_path / review_path,
        command=[
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write({message!r}); raise SystemExit(9)",
        ],
        timeout_seconds=10,
    )

    assert result.classification is not None
    assert result.classification.kind is expected_kind
    assert result.exit_code == 9
    assert result.process_exit_code is None
    assert result.produced_expected_deliverable is False


def test_agy_zero_exit_is_unaffected_by_post_completion_rule(
    tmp_path: Path,
) -> None:
    review_path = "reviews/not-required-for-zero-exit.md"
    result = run_cli(
        cli="gemini",
        prompt="Prompt prose is not completion evidence.",
        cwd=tmp_path,
        expected_deliverable_path=tmp_path / review_path,
        command=[
            sys.executable,
            "-c",
            (
                "import sys; print('review complete'); "
                "sys.stderr.write('Error: timeout waiting for response\\n')"
            ),
        ],
        timeout_seconds=10,
    )

    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.SUCCESS
    assert result.classification.observed_exit_code is None
    assert result.exit_code == 0
    assert result.process_exit_code is None
    assert result.produced_expected_deliverable is False


def test_antigravity_invocation_records_fixed_effective_model_without_model_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_DISPATCH_GEMINI_CMD", raising=False)

    invocation = resolve_invocation(
        cli="gemini",
        cwd=tmp_path,
        model="gemini-3.1-pro-preview",
        reasoning_effort=None,
        requested_model="gemini/3.1-pro-preview",
        resolved_via="model_registry:gemini/3.1-pro-preview",
        prompt="Review the assigned change.",
    )

    evidence = invocation.review_model_evidence.to_dict()
    assert Path(invocation.command[0]).name == "agy"
    assert "--model" not in invocation.command
    assert evidence == {
        "schema": "atlas-dispatch.review-model-evidence",
        "schema_version": 1,
        "effective_cli": "agy",
        "effective_model": "gemini/3.6-flash-agy",
        "model_resolution_source": "fixed-runtime-declaration",
        "requested_model": "gemini/3.1-pro-preview",
    }


def test_codex_invocation_records_canonical_registry_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_DISPATCH_CODEX_CMD", raising=False)

    invocation = resolve_invocation(
        cli="codex",
        cwd=tmp_path,
        model="gpt-5.6-sol",
        reasoning_effort="high",
        requested_model="codex/gpt-5.6-sol-high",
        resolved_via="model_registry:codex/gpt-5.6-sol-high",
        prompt="Review the assigned change.",
    )

    evidence = invocation.review_model_evidence.to_dict()
    assert evidence["effective_cli"] == "codex"
    assert evidence["effective_model"] == "codex/gpt-5.6-sol-high"
    assert evidence["model_resolution_source"] == "resolved-registry-argv"
    assert "requested_model" not in evidence
    assert invocation.command[invocation.command.index("-m") + 1] == "gpt-5.6-sol"


def test_invocation_without_established_runtime_model_is_explicit_unknown(
    tmp_path: Path,
) -> None:
    invocation = resolve_invocation(
        cli="unregistered-runtime",
        cwd=tmp_path,
        model=None,
        reasoning_effort=None,
        requested_model=None,
        resolved_via="explicit",
    )

    assert invocation.review_model_evidence.to_dict() == {
        "schema": "atlas-dispatch.review-model-evidence",
        "schema_version": 1,
        "effective_cli": "unregistered-runtime",
        "effective_model": "unknown",
        "model_resolution_source": "unread-runtime-model",
    }
def test_kimi_wire_content_part_text_is_captured(tmp_path: Path) -> None:
    result, _process = _run_wire_with_lines(
        tmp_path,
        [
            _wire_init_response(),
            _wire_line(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "ContentPart",
                        "payload": {"type": "text", "text": "visible"},
                    },
                }
            ),
            _wire_prompt_finished(),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "visible"


def test_kimi_wire_content_part_think_is_dropped(tmp_path: Path) -> None:
    result, _process = _run_wire_with_lines(
        tmp_path,
        [
            _wire_init_response(),
            _wire_line(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "ContentPart",
                        "payload": {"type": "think", "text": "hidden"},
                    },
                }
            ),
            _wire_line(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "ContentPart",
                        "payload": {"type": "text", "text": "shown"},
                    },
                }
            ),
            _wire_prompt_finished(),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "shown"


@pytest.mark.parametrize("response_id", ["1", "2"])
def test_kimi_wire_error_responses_are_reported(
    tmp_path: Path,
    response_id: str,
) -> None:
    lines = [
        _wire_line(
            {
                "jsonrpc": "2.0",
                "id": response_id,
                "error": {"code": -32000, "message": "bad wire request"},
            }
        )
    ]
    if response_id == "2":
        lines.insert(0, _wire_init_response())

    result, _process = _run_wire_with_lines(tmp_path, lines)

    assert result.exit_code == 1
    assert "kimi wire error -32000: bad wire request" in result.stderr


def test_kimi_wire_timeout_drains_already_queued_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_text = _wire_line(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "ContentPart",
                "payload": {"type": "text", "text": "queued before timeout"},
            },
        }
    )
    process = _FakeProcess([queued_text], block=False)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    monkeypatch.setattr("atlas_dispatch.adapter.KIMI_WIRE_QUEUE_POLL_SECONDS", 0.001)
    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi-streaming",
            prompt="slow prompt",
            cwd=tmp_path,
            model="kimi-code/kimi-for-coding",
            reasoning_effort="--thinking",
            timeout_seconds=0,
        )

    assert result.timed_out
    assert result.stdout == "queued before timeout"


def test_kimi_wire_terminate_escalates_to_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(block=True, wait_timeout_once=True)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    monkeypatch.setattr("atlas_dispatch.adapter.KIMI_WIRE_QUEUE_POLL_SECONDS", 0.001)
    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi-streaming",
            prompt="slow prompt",
            cwd=tmp_path,
            model="kimi-code/kimi-for-coding",
            reasoning_effort="--thinking",
            timeout_seconds=0,
        )

    assert result.timed_out
    assert process.terminated
    assert process.killed


def test_claude_dispatch_argv_grants_full_access_so_reviewers_can_run_tests() -> None:
    """Claude must get full-access flags like every other dispatch CLI.

    Regression guard. ``--permission-mode acceptEdits`` auto-accepts *edits*
    but still gates Bash. In ``--print`` mode there is nobody to grant that,
    so every ``python`` invocation is denied: a reviewer cannot create a venv
    or run the suite, and can only report that it executed no tests. A
    reviewer that cannot execute cannot verify by effect.

    The task-authored path lists predict the diff surface; they are not a CLI
    sandbox or safety gate. The CLI still needs full access to execute tests.
    """
    from atlas_dispatch.adapter import CLIS

    argv = CLIS["claude"].argv_template

    assert "--dangerously-skip-permissions" in argv
    # The half-measure permission mode must not come back.
    assert "acceptEdits" not in argv
    assert "--permission-mode" not in argv
    # Still scoped to the worktree the harness hands it.
    assert argv[argv.index("--add-dir") + 1] == "{cwd}"


def test_every_dispatch_cli_with_a_permission_flag_grants_full_access() -> None:
    """Parity guard: no dispatch CLI may be quietly the restricted one.

    Claude was the odd one out for an unknown period -- codex and gemini both
    carried full-access flags while claude carried a half-measure, and the
    asymmetry was invisible until a reviewer reported it could not run python.
    This pins the *set*, so adding a restricted CLI is a deliberate act.
    """
    from atlas_dispatch.adapter import CLIS

    full_access_markers = (
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-skip-permissions",
    )
    restricted: list[str] = []
    for name in ("codex", "gemini", "claude"):
        argv = CLIS[name].argv_template
        if not any(marker in argv for marker in full_access_markers):
            restricted.append(name)

    assert not restricted, (
        f"dispatch CLIs lacking a full-access flag: {restricted}. "
        "Worktree isolation + allowed_paths is the safety boundary; a CLI "
        "sandbox that blocks test execution makes its reviews source-traced "
        "only."
    )


def test_antigravity_evidence_refuses_the_constant_when_argv_carries_a_model_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Gemini constant is only true while the invocation carries NO model flag.

    agy 1.1.7 SILENTLY falls back to GPT-OSS 120B for every explicit --model value
    (argv_template comment, adapter.py). That is a GPT-FAMILY model -- the CODEX
    BUILDER's own family -- so a fallback would make the "independent" review leg
    correlated with the builder while every artifact still read
    "gemini/3.6-flash-agy".

    The invariant used to live in a COMMENT and the template's shape: the evidence
    branch DECLARED the model and never OBSERVED the rendered command, unlike its
    sibling which gates on `model and model in command`. Nothing failed when it was
    violated, and the failure was CORRELATED WITH THE PROPERTY BEING MEASURED.

    THIS IS THE POSITIVE CONTROL FOR THAT BRANCH. The sibling test above passes
    today only because the template happens to be clean; it CANNOT fail if someone
    reintroduces --model. This one must. Deleting it returns the invariant to being
    a comment.
    """

    monkeypatch.delenv("ATLAS_DISPATCH_GEMINI_CMD", raising=False)
    # Dirty the template exactly the way a registry edit or a new code path would.
    monkeypatch.setenv(
        "ATLAS_DISPATCH_GEMINI_CMD",
        "agy --dangerously-skip-permissions --model {model} -p {prompt}",
    )

    invocation = resolve_invocation(
        cli="gemini",
        cwd=tmp_path,
        model="gemini-3.1-pro-preview",
        reasoning_effort=None,
        requested_model="gemini/3.1-pro-preview",
        resolved_via="model_registry:gemini/3.1-pro-preview",
        prompt="Review the assigned change.",
    )

    evidence = invocation.review_model_evidence.to_dict()
    assert "--model" in invocation.command, (
        "precondition: this test only means anything if the rendered argv really "
        "carries the model flag"
    )
    # The constant must NOT be asserted for a run whose model is unverifiable.
    assert evidence["effective_model"] != "gemini/3.6-flash-agy"
    assert evidence["effective_model"] == UNKNOWN_EFFECTIVE_MODEL
    assert (
        evidence["model_resolution_source"]
        == "agy-model-flag-present-runtime-unverifiable"
    )


def test_agy_model_guard_is_token_exact_so_a_prompt_discussing_model_does_not_trip_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUST-NOT-FIRE: the prompt is ONE argv element and may legitimately discuss flags.

    The guard exists to detect a real `--model` selection. A substring scan would
    misfire on exactly the reviews it protects: a reviewer asked to check "this CLI
    must never be passed --model" would have its own evidence downgraded to unknown,
    turning a correct review into a gate failure.

    Deleting this lets the guard be "hardened" into a substring match, which reads
    as stricter and is strictly worse.
    """

    monkeypatch.delenv("ATLAS_DISPATCH_GEMINI_CMD", raising=False)

    invocation = resolve_invocation(
        cli="gemini",
        cwd=tmp_path,
        model="gemini-3.1-pro-preview",
        reasoning_effort=None,
        requested_model="gemini/3.1-pro-preview",
        resolved_via="model_registry:gemini/3.1-pro-preview",
        prompt=(
            "Review this diff. The agy CLI must never be passed --model, "
            "--model=gemini-3.6-pro, or -m: every explicit value silently falls "
            "back to a GPT-family model."
        ),
    )

    evidence = invocation.review_model_evidence.to_dict()
    assert "--model" not in invocation.command, (
        "precondition: the rendered argv itself must be clean; only the prompt "
        "text mentions the flag"
    )
    assert evidence["effective_model"] == "gemini/3.6-flash-agy"
    assert evidence["model_resolution_source"] == "fixed-runtime-declaration"
