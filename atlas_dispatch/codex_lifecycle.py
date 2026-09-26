"""Codex exec JSONL lifecycle parsing.

The Codex CLI's `exec --json` mode emits one JSON object per stdout line.
This module keeps the parsing and state tracking independent from the
subprocess adapter so tests can exercise lifecycle behavior without starting
Codex.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Literal

TurnStatus = Literal["completed", "failed", "interrupted", "inProgress"]
TerminalClassification = Literal[
    "success",
    "failed",
    "stalled",
    "interrupted",
    "overloaded",
    "approval_blocked",
    "unknown",
]


@dataclass(frozen=True, kw_only=True)
class TurnStarted:
    event: str
    timestamp_utc: str
    payload_keys: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ItemStarted:
    event: str
    timestamp_utc: str
    payload_keys: tuple[str, ...]
    item: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class ItemCompleted:
    event: str
    timestamp_utc: str
    payload_keys: tuple[str, ...]
    item: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class TurnCompleted:
    event: str
    timestamp_utc: str
    payload_keys: tuple[str, ...]
    status: TurnStatus
    error: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    event: str
    timestamp_utc: str
    payload_keys: tuple[str, ...]
    command: str | None
    exit_code: int | None
    output: str


@dataclass(frozen=True, kw_only=True)
class TokenUsageUpdated:
    event: str
    timestamp_utc: str
    payload_keys: tuple[str, ...]
    usage: dict[str, int]


CodexTypedEvent = (
    TurnStarted
    | ItemStarted
    | ItemCompleted
    | TurnCompleted
    | CommandResult
    | TokenUsageUpdated
)


@dataclass(kw_only=True)
class CodexLifecycleState:
    current_turn_status: TurnStatus | None = None
    last_event_at: float | None = None
    accumulated_token_usage: dict[str, int] = field(default_factory=dict)
    error_info: dict[str, Any] | None = None
    approval_required: bool = False


def utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def decode_jsonl_line(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def compact_lifecycle_record(
    payload: dict[str, Any],
    *,
    timestamp_utc: str,
) -> dict[str, Any]:
    return {
        "event": event_name(payload),
        "timestamp_utc": timestamp_utc,
        "payload_keys": payload_keys(payload),
    }


def apply_codex_event(
    payload: dict[str, Any],
    state: CodexLifecycleState,
    *,
    received_at: float,
    timestamp_utc: str,
) -> list[CodexTypedEvent]:
    """Update `state` from one decoded Codex JSONL payload."""

    state.last_event_at = received_at
    event = event_name(payload)
    normalized = _normalize_event_name(event)
    keys = payload_keys(payload)
    typed: list[CodexTypedEvent] = []

    if _is_approval_required_event(payload, normalized):
        state.approval_required = True

    if normalized == "turn.started":
        state.current_turn_status = "inProgress"
        typed.append(
            TurnStarted(event=event, timestamp_utc=timestamp_utc, payload_keys=keys)
        )
        return typed

    if normalized == "item.started":
        item = _dict_value(payload.get("item"))
        typed.append(
            ItemStarted(
                event=event,
                timestamp_utc=timestamp_utc,
                payload_keys=keys,
                item=item,
            )
        )
        return typed

    if normalized == "item.updated":
        # Progress within an item; it proves liveness (last_event_at, set
        # above) but changes no lifecycle state.
        return typed

    if normalized == "item.completed":
        item = _dict_value(payload.get("item"))
        typed.append(
            ItemCompleted(
                event=event,
                timestamp_utc=timestamp_utc,
                payload_keys=keys,
                item=item,
            )
        )
        if item.get("type") == "command_execution":
            typed.append(
                CommandResult(
                    event=event,
                    timestamp_utc=timestamp_utc,
                    payload_keys=keys,
                    command=_optional_str(item.get("command")),
                    exit_code=_optional_int(item.get("exit_code")),
                    output=str(item.get("aggregated_output") or ""),
                )
            )
        return typed

    if normalized in {"turn.completed", "turn.failed", "turn.interrupted"}:
        status = _turn_status_from_payload(payload, normalized)
        state.current_turn_status = status
        error_info = extract_error_info(payload)
        if status == "completed":
            state.error_info = None
        elif error_info is not None:
            state.error_info = error_info
        typed.append(
            TurnCompleted(
                event=event,
                timestamp_utc=timestamp_utc,
                payload_keys=keys,
                status=status,
                error=error_info,
            )
        )
        usage = extract_token_usage(payload.get("usage"))
        if usage is not None:
            state.accumulated_token_usage = usage
            typed.append(
                TokenUsageUpdated(
                    event=event,
                    timestamp_utc=timestamp_utc,
                    payload_keys=keys,
                    usage=usage,
                )
            )
        return typed

    if normalized.endswith("tokenusage.updated") or normalized.endswith(
        "token_usage.updated"
    ):
        usage = extract_token_usage(
            payload.get("tokenUsage")
            or payload.get("token_usage")
            or payload.get("usage")
        )
        if usage is not None:
            state.accumulated_token_usage = usage
            typed.append(
                TokenUsageUpdated(
                    event=event,
                    timestamp_utc=timestamp_utc,
                    payload_keys=keys,
                    usage=usage,
                )
            )
        return typed

    if normalized == "error":
        error_info = extract_error_info(payload) or {
            "message": str(payload.get("message") or "unknown Codex error"),
            "httpStatusCode": None,
            "errorCode": None,
        }
        state.error_info = error_info
        state.current_turn_status = "failed"
        typed.append(
            TurnCompleted(
                event=event,
                timestamp_utc=timestamp_utc,
                payload_keys=keys,
                status="failed",
                error=error_info,
            )
        )

    return typed


def classify_terminal_state(
    state: CodexLifecycleState,
    *,
    now: float | None = None,
    idle_timeout_seconds: float = 900.0,
) -> TerminalClassification:
    if state.approval_required:
        return "approval_blocked"
    if _is_overloaded_error(state.error_info):
        return "overloaded"
    if state.current_turn_status == "completed":
        return "success"
    if state.current_turn_status == "failed":
        return "failed"
    if state.current_turn_status == "interrupted":
        return "interrupted"
    if (
        state.current_turn_status in (None, "inProgress")
        and state.last_event_at is not None
        and now is not None
        and now - state.last_event_at > idle_timeout_seconds
    ):
        return "stalled"
    return "unknown"


def event_name(payload: dict[str, Any]) -> str:
    raw = payload.get("type") or payload.get("method") or "unknown"
    return str(raw)


def payload_keys(payload: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(str(key) for key in payload if key != "type"))


def extract_token_usage(raw_usage: Any) -> dict[str, int] | None:
    if not isinstance(raw_usage, dict):
        return None

    usage = raw_usage
    total_breakdown = raw_usage.get("total")
    if isinstance(total_breakdown, dict):
        usage = total_breakdown

    input_tokens = _first_int(
        usage,
        "input",
        "input_tokens",
        "inputTokens",
        "prompt_tokens",
        "promptTokens",
    )
    output_tokens = _first_int(
        usage,
        "output",
        "output_tokens",
        "outputTokens",
        "completion_tokens",
        "completionTokens",
    )
    total_tokens = _first_int(usage, "total", "total_tokens", "totalTokens")

    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None

    input_value = input_tokens or 0
    output_value = output_tokens or 0
    total_value = total_tokens if total_tokens is not None else input_value + output_value
    return {
        "input": input_value,
        "output": output_value,
        "total": total_value,
    }


def extract_error_info(payload: dict[str, Any]) -> dict[str, Any] | None:
    sources: list[dict[str, Any]] = []
    for key in ("error", "codexErrorInfo", "codex_error_info", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            sources.append(value)

    nested_error = payload.get("error")
    if isinstance(nested_error, dict):
        for key in ("codexErrorInfo", "codex_error_info", "data"):
            value = nested_error.get(key)
            if isinstance(value, dict):
                sources.append(value)
    sources.append(payload)

    message = _first_value(sources, "message")
    http_status = _first_value(sources, "httpStatusCode", "http_status_code", "status")
    error_code = _first_value(
        sources,
        "errorCode",
        "error_code",
        "code",
    )

    if message is None and http_status is None and error_code is None:
        return None

    return {
        "message": str(message or ""),
        "httpStatusCode": http_status,
        "errorCode": error_code,
    }


def _turn_status_from_payload(
    payload: dict[str, Any],
    normalized_event: str,
) -> TurnStatus:
    raw_status = payload.get("status")
    if raw_status in {"completed", "failed", "interrupted", "inProgress"}:
        return raw_status  # type: ignore[return-value]
    if normalized_event == "turn.failed":
        return "failed"
    if normalized_event == "turn.interrupted":
        return "interrupted"
    return "completed"


def _normalize_event_name(value: str) -> str:
    normalized = value.replace("/", ".").replace("_", ".")
    return normalized.strip(".")


def _is_approval_required_event(
    payload: dict[str, Any],
    normalized_event: str,
) -> bool:
    lowered = normalized_event.lower()
    if "approval" in lowered and (
        "required" in lowered
        or "requestapproval" in lowered
        or "request.approval" in lowered
    ):
        return True

    item = payload.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type") or "").lower()
        if "approval" in item_type and ("required" in item_type or "request" in item_type):
            return True
    return False


def _is_overloaded_error(error_info: dict[str, Any] | None) -> bool:
    if not error_info:
        return False
    code = error_info.get("errorCode")
    message = str(error_info.get("message") or "").lower()
    return str(code) == "-32001" or "server_overloaded" in message or "overloaded" in message


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in mapping:
            continue
        value = _optional_int(mapping.get(key))
        if value is not None:
            return value
    return None


def _first_value(sources: list[dict[str, Any]], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
    return None
