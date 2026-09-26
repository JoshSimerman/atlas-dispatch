"""Kimi adapter regression tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from atlas_dispatch.adapter import (
    DispatchErrorKind,
    render_command,
    resolve_model,
    run_cli,
)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _BlockingStream:
    def __init__(self, process: _FakeProcess) -> None:
        self.process = process

    def __iter__(self) -> _BlockingStream:
        return self

    def __next__(self) -> str:
        while self.process.returncode is None:
            time.sleep(0.001)
        raise StopIteration


class _ImmediateEOFStream:
    def __init__(self, eof_seen: threading.Event) -> None:
        self.eof_seen = eof_seen
        self.eof_reported = False

    def __iter__(self) -> _ImmediateEOFStream:
        return self

    def __next__(self) -> str:
        if not self.eof_reported:
            self.eof_reported = True
            self.eof_seen.set()
        raise StopIteration


class _DelayedUnsupportedWireStream:
    def __init__(self, stdout_eof_seen: threading.Event) -> None:
        self.stdout_eof_seen = stdout_eof_seen
        self.lines = ["Usage: kimi [OPTIONS]\n", "No such option: --wire\n"]

    def __iter__(self) -> _DelayedUnsupportedWireStream:
        return self

    def __next__(self) -> str:
        if not self.lines:
            raise StopIteration
        if not self.stdout_eof_seen.wait(timeout=1):
            raise AssertionError("stdout EOF was not observed before stderr read")
        time.sleep(0.03)
        return self.lines.pop(0)


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str] | None = None,
        stderr_lines: list[str] | None = None,
        *,
        block: bool = False,
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self.returncode: int | None = None
        self.final_returncode = returncode
        self.terminated = False
        self.killed = False
        self.stdout = _BlockingStream(self) if block else iter(stdout_lines or [])
        self.stderr = iter(stderr_lines or [])

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _wire_line(payload: dict[str, object]) -> str:
    return json.dumps(payload) + "\n"


def test_existing_kimi_models_stay_single_shot(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    processes: list[_FakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        calls.append({"command": command, **kwargs})
        process = _FakeProcess(returncode=0)
        processes.append(process)
        kwargs["stdout"].write("done")  # type: ignore[index, union-attr]
        kwargs["stderr"].write("")  # type: ignore[index, union-attr]
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi",
            prompt="hello",
            cwd=tmp_path,
            model="kimi-code/kimi-for-coding",
            reasoning_effort="--thinking",
            timeout_seconds=10,
        )

    assert result.exit_code == 0
    assert processes[0].stdin.writes == ["hello"]
    assert "--print" in calls[0]["command"]
    assert "--output-format" in calls[0]["command"]
    assert "text" in calls[0]["command"]
    assert "--wire" not in calls[0]["command"]


def test_kimi_registry_entries_use_node_kimi_code_shape(tmp_path: Path) -> None:
    high = resolve_model("kimi/k2.7-coding")
    default = resolve_model("kimi/k2.7-coding-highspeed")

    assert high.cli == "kimi-code"
    assert default.cli == "kimi-code"
    assert high.reasoning_effort is None
    assert default.reasoning_effort is None

    for model in (high, default):
        command = render_command(
            cli=model.cli,
            cwd=tmp_path,
            model=model.model_id,
            reasoning_effort=model.reasoning_effort,
            prompt="hello",
        )

        assert command[command.index("-p") + 1] == "hello"
        assert command[command.index("-m") + 1] == model.model_id
        assert "--print" not in command
        assert "--wire" not in command


def test_kimi_high_streaming_model_entry_is_removed() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        resolve_model("kimi/k2.6-high-streaming")


def test_kimi_streaming_driver_captures_incremental_text(tmp_path: Path) -> None:
    lines = [
        _wire_line({"jsonrpc": "2.0", "id": "1", "result": {"protocol_version": "1.9"}}),
        _wire_line(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": "TextPart", "payload": {"text": "hello "}},
            }
        ),
        _wire_line(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": "TextPart", "payload": {"text": "world"}},
            }
        ),
        _wire_line({"jsonrpc": "2.0", "id": "2", "result": {"status": "finished"}}),
    ]
    processes: list[_FakeProcess] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        process = _FakeProcess(lines)
        processes.append(process)
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi-streaming",
            prompt="say hello",
            cwd=tmp_path,
            model="kimi-code/kimi-for-coding",
            reasoning_effort="--thinking",
            timeout_seconds=10,
        )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "hello world"
    assert "--wire" in result.command
    stdin_payload = "".join(processes[0].stdin.writes)
    assert '"method":"initialize"' in stdin_payload
    assert '"method":"prompt"' in stdin_payload
    assert "say hello" in stdin_payload


def test_kimi_streaming_timeout_cancels_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(block=True)

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
    assert result.exit_code == -1
    assert result.classification is not None
    assert result.classification.kind is DispatchErrorKind.TIMEOUT
    assert process.terminated
    assert '"method":"cancel"' in "".join(process.stdin.writes)


def test_kimi_streaming_mode_not_supported_fails_loud(tmp_path: Path) -> None:
    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        process = _FakeProcess(
            stderr_lines=["Usage: kimi [OPTIONS]\n", "No such option: --wire\n"],
            returncode=2,
        )
        process.returncode = 2
        return process

    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi-streaming",
            prompt="hello",
            cwd=tmp_path,
            model="kimi-code/kimi-for-coding",
            reasoning_effort="--thinking",
            timeout_seconds=10,
        )

    assert result.exit_code == 2
    assert "requested Kimi streaming mode" in result.stderr
    assert "does not support `--wire`" in result.stderr
    assert "--print" not in result.command


def test_unsupported_wire_diagnostic_when_stderr_arrives_after_stdout_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_eof_seen = threading.Event()
    process = _FakeProcess(returncode=2)
    process.returncode = 2
    process.stdout = _ImmediateEOFStream(stdout_eof_seen)
    process.stderr = _DelayedUnsupportedWireStream(stdout_eof_seen)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return process

    monkeypatch.setattr("atlas_dispatch.adapter.KIMI_WIRE_QUEUE_POLL_SECONDS", 0.001)
    with patch("atlas_dispatch.adapter.subprocess.Popen", fake_popen):
        result = run_cli(
            cli="kimi-streaming",
            prompt="hello",
            cwd=tmp_path,
            model="kimi-code/kimi-for-coding",
            reasoning_effort="--thinking",
            timeout_seconds=10,
        )

    assert result.exit_code == 2
    assert "No such option: --wire" in result.stderr
    assert "requested Kimi streaming mode" in result.stderr
    assert "does not support `--wire`" in result.stderr
    assert "--print" not in result.command
