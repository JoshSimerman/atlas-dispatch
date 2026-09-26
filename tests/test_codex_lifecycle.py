from __future__ import annotations

import json

from atlas_dispatch.codex_lifecycle import (
    CodexLifecycleState,
    CommandResult,
    ItemCompleted,
    TokenUsageUpdated,
    apply_codex_event,
    classify_terminal_state,
    decode_jsonl_line,
)


def _apply(payload: dict[str, object], state: CodexLifecycleState, at: float):
    return apply_codex_event(
        payload,
        state,
        received_at=at,
        timestamp_utc="2026-05-19T00:00:00Z",
    )


def test_codex_lifecycle_parses_successful_exec_json_stream() -> None:
    state = CodexLifecycleState()
    lines = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "/bin/zsh -lc pwd",
                "aggregated_output": "/tmp/work\n",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "Done."},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 7}},
    ]

    typed = []
    for index, payload in enumerate(lines):
        decoded = decode_jsonl_line(json.dumps(payload))
        assert decoded is not None
        typed.extend(_apply(decoded, state, float(index)))

    assert classify_terminal_state(state, now=5.0) == "success"
    assert state.current_turn_status == "completed"
    assert state.accumulated_token_usage == {"input": 5, "output": 7, "total": 12}
    assert any(isinstance(event, CommandResult) for event in typed)
    assert any(isinstance(event, ItemCompleted) for event in typed)
    assert any(isinstance(event, TokenUsageUpdated) for event in typed)


def test_codex_lifecycle_classifies_idle_in_progress_turn_as_stalled() -> None:
    state = CodexLifecycleState()
    _apply({"type": "turn.started"}, state, 10.0)

    assert (
        classify_terminal_state(state, now=71.0, idle_timeout_seconds=60)
        == "stalled"
    )


def test_codex_lifecycle_classifies_no_event_idle_as_stalled() -> None:
    state = CodexLifecycleState(last_event_at=10.0)

    assert (
        classify_terminal_state(state, now=71.0, idle_timeout_seconds=60)
        == "stalled"
    )


def test_codex_lifecycle_no_event_within_idle_timeout_is_unknown() -> None:
    state = CodexLifecycleState(last_event_at=10.0)

    assert (
        classify_terminal_state(state, now=70.0, idle_timeout_seconds=60)
        == "unknown"
    )


def test_codex_lifecycle_classifies_overload_error_code() -> None:
    state = CodexLifecycleState()
    _apply(
        {
            "type": "turn.failed",
            "error": {
                "message": "Server overloaded; retry later.",
                "code": -32001,
            },
        },
        state,
        1.0,
    )

    assert state.error_info == {
        "message": "Server overloaded; retry later.",
        "httpStatusCode": None,
        "errorCode": -32001,
    }
    assert classify_terminal_state(state, now=2.0) == "overloaded"


def test_codex_lifecycle_clean_completion_clears_transient_error() -> None:
    state = CodexLifecycleState()
    _apply(
        {
            "type": "error",
            "message": "Server overloaded; retry later.",
            "code": -32001,
        },
        state,
        1.0,
    )
    _apply({"type": "turn.completed"}, state, 2.0)

    assert state.error_info is None
    assert classify_terminal_state(state, now=3.0) == "success"


def test_codex_lifecycle_marks_approval_required_for_triage() -> None:
    state = CodexLifecycleState()
    _apply({"type": "turn.started"}, state, 1.0)
    _apply(
        {
            "type": "approval_required",
            "item": {"id": "item_0", "type": "approval_request"},
        },
        state,
        2.0,
    )

    assert classify_terminal_state(state, now=3.0) == "approval_blocked"
