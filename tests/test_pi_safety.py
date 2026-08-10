import hashlib
import json

from app.pi_safety import (
    completed_pi_tool_calls,
    confirmed_pi_memory_write_receipt,
    has_any_pi_tool_event,
    make_read_only_without_tools,
    set_pi_tools,
)


def event(**payload: object) -> str:
    return json.dumps(payload)


def test_completed_pi_tool_calls_requires_matching_start_end_pair():
    raw = "\n".join(
        [
            "not-json",
            event(
                type="tool_execution_start",
                toolCallId="call-1",
                toolName="memory_recall",
                args={"query": "fact"},
            ),
            event(
                type="tool_execution_end",
                toolCallId="call-1",
                toolName="memory_recall",
                result={"memories": []},
                isError=False,
            ),
        ]
    )

    assert completed_pi_tool_calls(raw) == [
        {
            "type": "pi_tool_call",
            "call_id": "call-1",
            "tool": "memory_recall",
            "arguments": {"query": "fact"},
            "result": {"memories": []},
            "isError": False,
        }
    ]


def test_completed_pi_tool_calls_rejects_mismatch_or_duplicate_call_id():
    mismatch = "\n".join(
        [
            event(
                type="tool_execution_start",
                toolCallId="call-1",
                toolName="memory_recall",
                args={"query": "fact"},
            ),
            event(
                type="tool_execution_end",
                toolCallId="call-1",
                toolName="memory_write",
                result={},
            ),
        ]
    )
    duplicate = "\n".join(
        [
            event(
                type="tool_execution_start",
                toolCallId="call-1",
                toolName="memory_recall",
                args={"query": "fact"},
            ),
            event(
                type="tool_execution_start",
                toolCallId="call-1",
                toolName="memory_recall",
                args={"query": "other"},
            ),
            event(
                type="tool_execution_end",
                toolCallId="call-1",
                toolName="memory_recall",
                result={"memories": []},
            ),
        ]
    )

    assert completed_pi_tool_calls(mismatch) == []
    assert completed_pi_tool_calls(duplicate) == []


def test_tool_event_detection_is_pi_only_and_includes_attempts():
    assert has_any_pi_tool_event(
        event(
            type="tool_execution_start",
            toolCallId="call-1",
            toolName="workspace_read",
        )
    )
    assert not has_any_pi_tool_event(
        event(
            type="item.completed",
            item={"type": "mcp_tool_call", "tool": "memory_recall"},
        )
    )


def test_set_pi_tools_replaces_existing_selection_before_image_arguments():
    command = [
        "node",
        "pi.js",
        "--tools",
        "workspace_read,memory_recall",
        "--session-id",
        "session-1",
        "@/tmp/image.png",
    ]

    set_pi_tools(command, ("memory_write",))

    assert command == [
        "node",
        "pi.js",
        "--session-id",
        "session-1",
        "--tools",
        "memory_write",
        "@/tmp/image.png",
    ]
    make_read_only_without_tools(command)
    assert "--tools" not in command
    assert command[-2:] == ["--no-tools", "@/tmp/image.png"]


def test_confirmed_memory_write_receipt_binds_operation_and_completion():
    arguments = {
        "data": "durable fact",
        "type": "text",
        "created_at": "2026-08-10",
    }
    digest = hashlib.sha256(
        json.dumps(
            {"tool": "memory_write", "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result = {
        "details": {
            "protocolVersion": 1,
            "bridge": "memory_connector",
            "effect": "write",
            "operation": "memory_write",
            "operationDigest": digest,
            "targetIdentifiers": {},
            "exitCode": 0,
            "completed": True,
            "safeToConfirm": True,
            "receipt": {
                "episode_uuid": "episode-1",
                "processing_status": "completed",
            },
        }
    }

    assert confirmed_pi_memory_write_receipt(result, arguments=arguments) == {
        "episode_uuid": "episode-1",
        "processing_status": "completed",
    }

    result["details"]["operationDigest"] = "wrong"
    assert confirmed_pi_memory_write_receipt(result, arguments=arguments) is None
