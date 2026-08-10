from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from app.config import forbidden_path_prefixes


PI_ASSISTANT_EVENT_TYPES = frozenset({"message_end", "turn_end"})
PI_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "agent_start",
        "agent_end",
        "agent_settled",
        "message_start",
        "message_update",
        "turn_start",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "auto_retry_start",
        "auto_retry_end",
        "compaction_start",
        "compaction_end",
        "extension_error",
    }
)


@dataclass(frozen=True)
class PiStreamSummary:
    saw_lifecycle: bool
    saw_agent_start: bool
    saw_agent_end: bool
    saw_agent_settled: bool
    retry_starts: int
    retry_ends: int
    compaction_starts: int
    compaction_ends: int
    extension_errors: tuple[str, ...]
    incomplete_tool_call_ids: tuple[str, ...]
    stream_invalid: bool = False


def summarize_pi_stream(raw: str) -> PiStreamSummary:
    """Summarize completion-critical events from Pi's JSONL print protocol."""
    saw_lifecycle = False
    saw_agent_start = False
    saw_agent_end = False
    saw_agent_settled = False
    retry_starts = 0
    retry_ends = 0
    compaction_starts = 0
    compaction_ends = 0
    extension_errors: list[str] = []
    active_tool_calls: dict[str, int] = {}
    missing_tool_call_sequence = 0
    stream_invalid = False

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if saw_lifecycle:
                stream_invalid = True
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            if saw_lifecycle:
                stream_invalid = True
            continue

        payload_type = payload["type"]
        if payload_type in PI_LIFECYCLE_EVENT_TYPES:
            saw_lifecycle = True
        if payload_type == "agent_start":
            saw_agent_start = True
        elif payload_type == "agent_end":
            saw_agent_end = True
        elif payload_type == "agent_settled":
            saw_agent_settled = True
        elif payload_type == "auto_retry_start":
            retry_starts += 1
        elif payload_type == "auto_retry_end":
            retry_ends += 1
        elif payload_type == "compaction_start":
            compaction_starts += 1
        elif payload_type == "compaction_end":
            compaction_ends += 1
        elif payload_type == "extension_error":
            extension_errors.append(_extension_error_text(payload))
        elif payload_type == "tool_execution_start":
            call_id = _string(payload.get("toolCallId")).strip()
            if not call_id:
                missing_tool_call_sequence += 1
                call_id = f"<missing:{missing_tool_call_sequence}>"
            active_tool_calls[call_id] = active_tool_calls.get(call_id, 0) + 1
        elif payload_type == "tool_execution_end":
            call_id = _string(payload.get("toolCallId")).strip()
            if call_id and active_tool_calls.get(call_id, 0) > 0:
                remaining = active_tool_calls[call_id] - 1
                if remaining:
                    active_tool_calls[call_id] = remaining
                else:
                    active_tool_calls.pop(call_id, None)

    incomplete_tool_call_ids = tuple(
        call_id
        for call_id in sorted(active_tool_calls)
        for _ in range(active_tool_calls[call_id])
    )
    return PiStreamSummary(
        saw_lifecycle=saw_lifecycle,
        saw_agent_start=saw_agent_start,
        saw_agent_end=saw_agent_end,
        saw_agent_settled=saw_agent_settled,
        retry_starts=retry_starts,
        retry_ends=retry_ends,
        compaction_starts=compaction_starts,
        compaction_ends=compaction_ends,
        extension_errors=tuple(extension_errors),
        incomplete_tool_call_ids=incomplete_tool_call_ids,
        stream_invalid=stream_invalid,
    )


def pi_stream_completion_issue(raw: str) -> str:
    """Return a stable failure code when a real Pi lifecycle did not settle safely."""
    summary = summarize_pi_stream(raw)
    if summary.extension_errors:
        return "pi_extension_failed"
    if summary.incomplete_tool_call_ids:
        return "pi_tool_incomplete"
    if summary.retry_starts > summary.retry_ends:
        return "pi_retry_incomplete"
    if summary.compaction_starts > summary.compaction_ends:
        return "pi_compaction_incomplete"
    if summary.stream_invalid:
        return "pi_stream_invalid"
    if summary.saw_lifecycle and not summary.saw_agent_settled:
        return "pi_not_settled"
    return ""


def assistant_text_candidates(payload: object) -> list[str]:
    """Return finalized assistant text emitted by Pi JSON event mode."""
    if not isinstance(payload, dict):
        return []
    if payload.get("type") not in PI_ASSISTANT_EVENT_TYPES:
        return []
    message = payload.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    return _message_text_candidates(message)


def pi_session_id_from_payload(payload: object) -> str | None:
    """Extract the session id from Pi's leading JSON session header."""
    if not isinstance(payload, dict) or payload.get("type") != "session":
        return None
    value = payload.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def pi_audit_events_from_payload(payload: object) -> list[dict[str, str]]:
    """Normalize Pi tool lifecycle and persisted message entries for auditing."""
    if not isinstance(payload, dict):
        return []
    payload_type = payload.get("type")
    if payload_type == "tool_execution_start":
        return [_pi_tool_start_event(payload)]
    if payload_type == "tool_execution_end":
        return [_pi_tool_end_event(payload)]
    if payload_type != "message":
        return []
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    return _pi_message_audit_events(message)


def _message_text_candidates(message: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        candidates.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                candidates.append(block["text"])
    return candidates


def _pi_tool_start_event(payload: dict[str, Any]) -> dict[str, str]:
    tool = _string(payload.get("toolName")) or "tool"
    call_id = _string(payload.get("toolCallId"))
    arguments = payload.get("args")
    event = {
        "event_type": "tool_execution_start",
        "tool": tool,
    }
    if call_id:
        event["call_id"] = call_id
    input_text = _json_text(arguments)
    if input_text:
        event["input"] = input_text
    command = _command_from_arguments(arguments)
    if command:
        event["command"] = command
    path = _path_from_arguments(arguments)
    if path:
        event["path"] = path
    return event


def _pi_tool_end_event(payload: dict[str, Any]) -> dict[str, str]:
    call_id = _string(payload.get("toolCallId"))
    output = _tool_result_text(payload.get("result"))
    event = {
        "event_type": "tool_execution_end",
        "tool": "tool_output",
    }
    if call_id:
        event["call_id"] = call_id
    if output:
        event["output"] = output
        path = _first_pathish_token(output)
        if path:
            event["path"] = path
    if payload.get("isError") is True:
        event["status"] = "failed"
    return event


def _pi_message_audit_events(message: dict[str, Any]) -> list[dict[str, str]]:
    role = message.get("role")
    if role == "assistant":
        events: list[dict[str, str]] = []
        content = message.get("content")
        if not isinstance(content, list):
            return events
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            event = _pi_tool_start_event(
                {
                    "toolName": block.get("name"),
                    "toolCallId": block.get("id"),
                    "args": block.get("arguments"),
                }
            )
            event["event_type"] = "message"
            events.append(event)
        return events
    if role == "toolResult":
        event = _pi_tool_end_event(
            {
                "toolCallId": message.get("toolCallId"),
                "result": message,
                "isError": message.get("isError"),
            }
        )
        event["event_type"] = "message"
        return [event]
    if role == "bashExecution":
        command = _string(message.get("command"))
        output = _string(message.get("output"))
        event = {"event_type": "message", "tool": "bash"}
        if command:
            event["command"] = command
            event["input"] = json.dumps({"command": command}, ensure_ascii=False)
        if output:
            event["output"] = output
        return [event]
    return []


def _tool_result_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return _json_text(value)
    content = value.get("content")
    if isinstance(content, list):
        texts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if texts:
            return "\n".join(texts)
    return _json_text(value)


def _command_from_arguments(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("cmd", "command"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return ""


def _path_from_arguments(value: object) -> str:
    if isinstance(value, dict):
        for key in ("path", "file", "filename"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return _first_pathish_token(_json_text(value))


def _first_pathish_token(text: str) -> str:
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.replace("\n", " ").split()
    for token in tokens:
        stripped = token.strip("'\"`[](),:;")
        if any(stripped.startswith(prefix) for prefix in forbidden_path_prefixes()):
            return stripped
        if stripped.endswith((".md", ".pdf", ".docx", ".xlsx")):
            return stripped
    return ""


def _json_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _extension_error_text(payload: dict[str, Any]) -> str:
    value = payload.get("error")
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())[:1000]
    return _json_text(value)[:1000] or "Pi extension failed"
