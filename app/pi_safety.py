from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator


WECHAT_MEMORY_READ_TOOLS = (
    "memory_get",
    "memory_recall",
    "timeline_get",
    "user_get",
)


def jsonl_payloads(raw: str) -> Iterator[dict[str, object]]:
    for line in raw.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def completed_pi_tool_calls(raw: str) -> list[dict[str, object]]:
    starts: dict[str, dict[str, object]] = {}
    invalid_call_ids: set[str] = set()
    completed: list[dict[str, object]] = []
    for payload in jsonl_payloads(raw):
        event_type = payload.get("type")
        call_id = str(payload.get("toolCallId") or "").strip()
        tool_name = str(payload.get("toolName") or "").strip()
        if event_type == "tool_execution_start" and call_id and tool_name:
            if call_id in starts or call_id in invalid_call_ids:
                starts.pop(call_id, None)
                invalid_call_ids.add(call_id)
                continue
            starts[call_id] = payload
            continue
        if event_type != "tool_execution_end" or not call_id:
            continue
        if call_id in invalid_call_ids:
            continue
        start = starts.pop(call_id, None)
        if not isinstance(start, dict):
            continue
        started_tool = str(start.get("toolName") or "").strip()
        if not started_tool or tool_name != started_tool:
            continue
        completed.append(
            {
                "type": "pi_tool_call",
                "call_id": call_id,
                "tool": started_tool,
                "arguments": start.get("args"),
                "result": payload.get("result"),
                "isError": payload.get("isError") is True,
            }
        )
    return completed


def confirmed_pi_memory_write_receipt(
    result: object,
    *,
    arguments: dict[str, object],
) -> dict[str, str] | None:
    if not isinstance(result, dict):
        return None
    details = result.get("details")
    if not isinstance(details, dict):
        return None
    canonical = json.dumps(
        {"tool": "memory_write", "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        details.get("protocolVersion") != 1
        or details.get("bridge") != "memory_connector"
        or details.get("effect") != "write"
        or details.get("operation") != "memory_write"
        or details.get("operationDigest") != expected_digest
        or details.get("targetIdentifiers") != {}
        or details.get("exitCode") != 0
        or details.get("completed") is not True
        or details.get("safeToConfirm") is not True
    ):
        return None
    receipt = details.get("receipt")
    if not isinstance(receipt, dict):
        return None
    episode_uuid = receipt.get("episode_uuid")
    if (
        not isinstance(episode_uuid, str)
        or not episode_uuid.strip()
        or receipt.get("processing_status") != "completed"
    ):
        return None
    return {
        "episode_uuid": episode_uuid.strip(),
        "processing_status": "completed",
    }


def has_any_pi_tool_event(raw: str) -> bool:
    return any(
        payload.get("type") in {"tool_execution_start", "tool_execution_end"}
        for payload in jsonl_payloads(raw)
    )


def make_read_only_without_tools(command: list[str]) -> None:
    set_pi_tools(command, None)


def set_pi_tools(command: list[str], tools: tuple[str, ...] | None) -> None:
    index = 0
    while index < len(command):
        if command[index] == "--no-tools":
            del command[index]
            continue
        if command[index] == "--tools":
            del command[index : index + 2]
            continue
        index += 1
    insert_at = next(
        (index for index, value in enumerate(command) if value.startswith("@")),
        len(command),
    )
    options = ["--no-tools"] if tools is None else ["--tools", ",".join(tools)]
    command[insert_at:insert_at] = options
