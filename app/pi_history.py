from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from app.pi_events import pi_audit_events_from_payload
from app.pi_runner import pi_session_dir


_PI_SESSION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
MAX_EVENT_BODY_CHARS = 20_000


@dataclass(frozen=True)
class RenderedPiEvent:
    timestamp: str
    kind: str
    title: str
    body: str
    expanded: bool = False


@dataclass(frozen=True)
class RenderedPiSession:
    session_id: str
    path: Path | None
    events: list[RenderedPiEvent]
    missing: bool = False


def render_local_pi_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
    max_events: int = 500,
) -> RenderedPiSession:
    path = find_pi_session_path(session_id, session_dir=session_dir)
    if path is None:
        return RenderedPiSession(
            session_id=session_id,
            path=None,
            events=[],
            missing=True,
        )
    events: list[RenderedPiEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if len(events) >= max_events:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.extend(_render_pi_payload(payload)[: max_events - len(events)])
    return RenderedPiSession(
        session_id=session_id,
        path=path,
        events=events,
    )


def find_pi_session_path(
    session_id: str,
    *,
    session_dir: Path | None = None,
) -> Path | None:
    normalized = session_id.strip()
    if not _PI_SESSION_ID_PATTERN.fullmatch(normalized):
        return None
    root = session_dir or pi_session_dir()
    if not root.is_dir():
        return None
    candidates = [root / f"{normalized}.jsonl"]
    candidates.extend(root.glob(f"*_{normalized}.jsonl"))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        if _pi_session_file_id(candidate) == normalized:
            return candidate
    return None


def count_pi_session_lines(
    session_id: str | None,
    *,
    session_dir: Path | None = None,
) -> int:
    if not session_id:
        return 0
    path = find_pi_session_path(session_id, session_dir=session_dir)
    if path is None:
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _line in handle)


def extract_pi_audit_events_from_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
    start_line: int = 0,
    end_line: int | None = None,
    limit: int = 40,
) -> list[dict[str, str]]:
    path = find_pi_session_path(session_id, session_dir=session_dir)
    if path is None:
        return []
    events: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in islice(handle, start_line, end_line):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            for event in pi_audit_events_from_payload(payload):
                events.append(event)
                if len(events) >= limit:
                    return events
    return events


def _pi_session_file_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict) or payload.get("type") != "session":
                    return None
                value = payload.get("id")
                return value.strip() if isinstance(value, str) and value.strip() else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


def _render_pi_payload(payload: Any) -> list[RenderedPiEvent]:
    if not isinstance(payload, dict):
        return []
    timestamp = _string(payload.get("timestamp"))
    if payload.get("type") == "session":
        fields = {
            "id": payload.get("id"),
            "cwd": payload.get("cwd"),
            "version": payload.get("version"),
        }
        return [
            RenderedPiEvent(
                timestamp=timestamp,
                kind="session",
                title="Pi session metadata",
                body="\n".join(
                    f"{key}: {value}" for key, value in fields.items() if value is not None
                ),
            )
        ]
    payload_type = payload.get("type")
    lifecycle_event = _render_lifecycle_event(timestamp, payload_type, payload)
    if lifecycle_event is not None:
        return [lifecycle_event]
    if payload_type != "message":
        return []
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    role = _string(message.get("role")) or "message"
    if role == "assistant":
        return _render_assistant_message(timestamp, message)
    if role == "toolResult":
        return [
            RenderedPiEvent(
                timestamp=timestamp,
                kind="tool_output",
                title=f"Tool output: {_string(message.get('toolName')) or 'tool'}",
                body=_truncate(_message_text(message)),
            )
        ]
    if role == "bashExecution":
        command = _string(message.get("command"))
        output = _string(message.get("output"))
        return [
            RenderedPiEvent(
                timestamp=timestamp,
                kind="tool_call",
                title="Bash",
                body=_truncate(f"$ {command}\n{output}".strip()),
            )
        ]
    text = _message_text(message)
    if not text:
        return []
    return [
        RenderedPiEvent(
            timestamp=timestamp,
            kind=role,
            title=role.title(),
            body=_truncate(text),
            expanded=role == "user",
        )
    ]


def _render_lifecycle_event(
    timestamp: str,
    payload_type: object,
    payload: dict[str, Any],
) -> RenderedPiEvent | None:
    if payload_type == "agent_start":
        return RenderedPiEvent(timestamp, "lifecycle", "Agent started", "")
    if payload_type == "agent_end":
        will_retry = payload.get("willRetry") is True
        return RenderedPiEvent(
            timestamp,
            "lifecycle",
            "Agent turn ended",
            f"will_retry: {str(will_retry).lower()}",
        )
    if payload_type == "agent_settled":
        return RenderedPiEvent(timestamp, "settled", "Agent settled", "")
    if payload_type == "auto_retry_start":
        fields = {
            "attempt": payload.get("attempt"),
            "max_attempts": payload.get("maxAttempts"),
            "delay_ms": payload.get("delayMs"),
            "error": payload.get("errorMessage"),
        }
        return RenderedPiEvent(
            timestamp,
            "retry",
            "Provider retry started",
            _field_lines(fields),
        )
    if payload_type == "auto_retry_end":
        fields = {
            "success": payload.get("success"),
            "attempt": payload.get("attempt"),
            "final_error": payload.get("finalError"),
        }
        return RenderedPiEvent(
            timestamp,
            "retry",
            "Provider retry ended",
            _field_lines(fields),
        )
    if payload_type == "compaction_start":
        return RenderedPiEvent(
            timestamp,
            "compaction",
            "Context compaction started",
            _field_lines({"reason": payload.get("reason")}),
        )
    if payload_type == "compaction_end":
        fields = {
            "reason": payload.get("reason"),
            "aborted": payload.get("aborted"),
            "will_retry": payload.get("willRetry"),
            "error": payload.get("errorMessage"),
        }
        return RenderedPiEvent(
            timestamp,
            "compaction",
            "Context compaction ended",
            _field_lines(fields),
        )
    if payload_type == "extension_error":
        fields = {
            "extension": payload.get("extensionPath"),
            "event": payload.get("event"),
            "error": payload.get("error"),
        }
        return RenderedPiEvent(
            timestamp,
            "error",
            "Pi extension error",
            _truncate(_field_lines(fields)),
            expanded=True,
        )
    if payload_type == "tool_execution_start":
        fields = {
            "call_id": payload.get("toolCallId"),
            "arguments": payload.get("args"),
        }
        return RenderedPiEvent(
            timestamp,
            "tool_call",
            f"Tool started: {_string(payload.get('toolName')) or 'tool'}",
            _truncate(_field_lines(fields)),
        )
    if payload_type == "tool_execution_end":
        fields = {
            "call_id": payload.get("toolCallId"),
            "is_error": payload.get("isError"),
            "result": payload.get("result"),
        }
        return RenderedPiEvent(
            timestamp,
            "tool_output",
            f"Tool ended: {_string(payload.get('toolName')) or 'tool'}",
            _truncate(_field_lines(fields)),
        )
    return None


def _render_assistant_message(
    timestamp: str,
    message: dict[str, Any],
) -> list[RenderedPiEvent]:
    events: list[RenderedPiEvent] = []
    content = message.get("content")
    if not isinstance(content, list):
        return events
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            events.append(
                RenderedPiEvent(
                    timestamp=timestamp,
                    kind="assistant",
                    title="Assistant",
                    body=_truncate(block["text"]),
                    expanded=True,
                )
            )
        elif block_type == "thinking" and isinstance(block.get("thinking"), str):
            events.append(
                RenderedPiEvent(
                    timestamp=timestamp,
                    kind="reasoning",
                    title="Thinking",
                    body=_truncate(block["thinking"]),
                )
            )
        elif block_type == "toolCall":
            arguments = block.get("arguments")
            body = json.dumps(arguments, ensure_ascii=False, indent=2)
            events.append(
                RenderedPiEvent(
                    timestamp=timestamp,
                    kind="tool_call",
                    title=f"Tool call: {_string(block.get('name')) or 'tool'}",
                    body=_truncate(body),
                )
            )
    return events


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _truncate(text: str) -> str:
    if len(text) <= MAX_EVENT_BODY_CHARS:
        return text
    return f"{text[:MAX_EVENT_BODY_CHARS]}\n...[truncated]"


def _field_lines(fields: dict[str, object]) -> str:
    lines: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        elif isinstance(value, bool):
            rendered = str(value).lower()
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
