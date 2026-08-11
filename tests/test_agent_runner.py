import json
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agent_context import AgentTaskContext
from app.agent_result import AgentOutcome
from app.agent_runner import (
    AGENT_RESULT_SCHEMA_PATH,
    AgentConversationLockedError,
    AgentRunUnknownError,
    AgentRunUnavailableError,
    DirectAgentRunner,
    ReconciliationProof,
    direct_agent_developer_instructions,
)
from app.process_runner import ProcessRunResult
from app.store import AutoReplyStore


def _task(store: AutoReplyStore):
    store.enqueue_reply_task(
        channel="dingtalk",
        conversation_id="cid",
        conversation_title="产品群",
        single_chat=False,
        trigger_message_id="mid",
        trigger_create_time="2026-07-28 12:00:00",
        trigger_sender="ET",
        trigger_text="修复并验证服务",
        execution_generation="generation-1",
    )
    return store.list_reply_tasks(statuses=("pending",), limit=1)[0]


def _context(task_id: int) -> AgentTaskContext:
    return AgentTaskContext(
        task_id=task_id,
        channel="dingtalk",
        conversation_id="cid",
        conversation_title="产品群",
        single_chat=False,
        trigger_message_id="mid",
        trigger_sender="ET",
        trigger_text="修复并验证服务",
        trigger_create_time="2026-07-28 12:00:00",
        messages=(),
        materials=(),
        prior_receipts=(),
    )


def _unknown_run(
    store: AutoReplyStore,
    task_id: int,
    *,
    native_cli: str = "dws",
    operation: str = "chat message send",
    target_identifiers: dict[str, str] | None = None,
):
    task = store.get_reply_task(task_id)
    assert task is not None
    if task.status == "pending":
        task = store.claim_reply_task(task_id, now="2026-07-29 08:58:59")
        assert task is not None
    claim = store.claim_agent_run(
        task.id,
        task.execution_generation,
        owner="seed-owner",
        lease_seconds=60,
        now="2026-07-29 08:59:00",
    )
    assert claim.claimed
    store.append_agent_run_event(
        claim.run.id,
        {
            "type": "item.started",
            "item": {
                "id": "write-1",
                "type": "command_execution",
                "metadata": {
                    "effect": "effectful",
                    "native_cli": native_cli,
                    "operation": operation,
                    "command_digest": "a" * 64,
                    "target_identifiers": target_identifiers
                    or {"conversation": "cid"},
                },
            },
        },
        owner="seed-owner",
        now="2026-07-29 08:59:01",
    )
    return store.mark_agent_run_unknown(
        claim.run.id,
        {"code": "pi_process_timeout", "retryable": True},
        owner="seed-owner",
        now="2026-07-29 08:59:02",
    )


def _result_line(
    *,
    outcome: str = "completed",
    summary: str = "修复已执行并验证。",
    code: str = "",
    retryable: bool = False,
) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(
                    {
                        "outcome": outcome,
                        "summary": summary,
                        "error": {
                            "code": code,
                            "retryable": retryable,
                            "authorization_required": False,
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        },
        ensure_ascii=False,
    )


def _jsonl(*, session_id: str = "session-1", outcome: str = "completed") -> str:
    return "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": session_id}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "read-1",
                        "type": "web_search_call",
                        "query": "current service status",
                    },
                }
            ),
            _result_line(
                outcome=outcome,
                summary="材料暂时不可用。" if outcome == "failed" else "修复已执行并验证。",
                code="material_unavailable" if outcome == "failed" else "",
                retryable=outcome == "failed",
            ),
        )
    )


def _pi_result_event(*, outcome: str = "completed") -> str:
    return json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "outcome": outcome,
                                "summary": "任务已执行。",
                                "error": {
                                    "code": "" if outcome != "failed" else "task_failed",
                                    "retryable": False,
                                    "authorization_required": False,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
        },
        ensure_ascii=False,
    )


def _pi_tool_event_jsonl(
    *,
    command: str,
    include_end: bool = True,
    tool_error: bool = False,
    outcome: str = "completed",
) -> str:
    argv = command.split()
    operation_digest = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    operation = "chat message send"
    targets = {"conversation": "cid"}
    events = [
        json.dumps({"type": "session", "id": "session-1"}),
        json.dumps({"type": "agent_start"}),
        json.dumps(
            {
                "type": "tool_execution_start",
                "toolCallId": "call-1",
                "toolName": "execute_reviewed_write",
                "args": {"argv": argv},
            }
        ),
    ]
    if include_end:
        events.append(
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1",
                    "toolName": "execute_reviewed_write",
                    "result": {
                        "content": [{"type": "text", "text": "done"}],
                        "details": {
                            "protocolVersion": 1,
                            "cli": "dws",
                            "effect": "write",
                            "operation": operation,
                            "operationDigest": operation_digest,
                            "targetIdentifiers": targets,
                            "exitCode": 0,
                            "completed": True,
                            "safeToConfirm": True,
                        },
                    },
                    "isError": tool_error,
                }
            )
        )
    events.append(_pi_result_event(outcome=outcome))
    if include_end:
        events.extend(
            [
                json.dumps({"type": "agent_end", "willRetry": False}),
                json.dumps({"type": "agent_settled"}),
            ]
        )
    return "\n".join(events)


def _pi_reconciliation_jsonl(*, include_read: bool = True) -> str:
    argv = ["dws", "chat", "message", "list", "--conversation", "cid"]
    operation_digest = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result_text = json.dumps({"messages": [{"id": "mid"}]})
    events: list[dict[str, object]] = [
        {"type": "session", "id": "reconciliation-session"},
        {"type": "agent_start"},
    ]
    if include_read:
        events.extend(
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "read-1",
                    "toolName": "execute_reviewed_read",
                    "args": {"argv": argv},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "read-1",
                    "toolName": "execute_reviewed_read",
                    "isError": False,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                        "details": {
                            "protocolVersion": 1,
                            "cli": "dws",
                            "effect": "read",
                            "operation": "chat message list",
                            "operationDigest": operation_digest,
                            "targetIdentifiers": {"conversation": "cid"},
                            "resultDigest": hashlib.sha256(
                                result_text.encode("utf-8")
                            ).hexdigest(),
                            "exitCode": 0,
                            "completed": True,
                            "safeToConfirm": False,
                        },
                    },
                },
            ]
        )
    events.extend(
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "outcome": "completed",
                                    "summary": "已从只读消息列表确认原发送存在。",
                                    "proof": {"observed_state": "effect_present"},
                                    "error": {
                                        "code": "",
                                        "retryable": False,
                                        "authorization_required": False,
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ],
                },
            },
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
    )
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _pi_memory_write_jsonl(*, safe_to_confirm: bool = True) -> str:
    arguments = {
        "data": "Friday prefers concise release notes.",
        "type": "text",
        "created_at": "2026-08-08T00:00:00Z",
    }
    operation_digest = hashlib.sha256(
        json.dumps(
            {"tool": "memory_write", "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    receipt = (
        {"episode_uuid": "episode-1", "processing_status": "completed"}
        if safe_to_confirm
        else {}
    )
    events = [
        {"type": "session", "id": "memory-session"},
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "memory-write-1",
            "toolName": "memory_write",
            "args": arguments,
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "memory-write-1",
            "toolName": "memory_write",
            "isError": False,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "episode_uuid": "episode-1",
                                "processing_status": "completed",
                            }
                        ),
                    }
                ],
                "details": {
                    "protocolVersion": 1,
                    "bridge": "memory_connector",
                    "effect": "write",
                    "operation": "memory_write",
                    "operationDigest": operation_digest,
                    "targetIdentifiers": {},
                    "resultDigest": "b" * 64,
                    "exitCode": 0,
                    "completed": True,
                    "safeToConfirm": safe_to_confirm,
                    "receipt": receipt,
                },
            },
        },
        json.loads(_pi_result_event()),
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    ]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _pi_document_upload_jsonl(*, include_processing_status: bool = True) -> str:
    arguments = {
        "filename": "report.txt",
        "content_base64": "cmVwb3J0",
    }
    operation_digest = hashlib.sha256(
        json.dumps(
            {"tool": "document_upload", "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    receipt = {"document_id": "document-1"}
    if include_processing_status:
        receipt["processing_status"] = "completed"
    events = [
        {"type": "session", "id": "document-session"},
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "document-upload-1",
            "toolName": "document_upload",
            "args": arguments,
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "document-upload-1",
            "toolName": "document_upload",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": "uploaded"}],
                "details": {
                    "protocolVersion": 1,
                    "bridge": "memory_connector",
                    "effect": "write",
                    "operation": "document_upload",
                    "operationDigest": operation_digest,
                    "targetIdentifiers": {},
                    "resultDigest": "c" * 64,
                    "exitCode": 0,
                    "completed": True,
                    "safeToConfirm": True,
                    "receipt": receipt,
                },
            },
        },
        json.loads(_pi_result_event()),
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    ]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _pi_xiaoqing_upload_jsonl(
    *,
    safe_to_confirm: bool = True,
    dry_run: bool = False,
) -> str:
    arguments = {
        "candidate_id": "candidate-1",
        "summary": "reviewed interview result",
        "dry_run": dry_run,
    }
    operation_digest = hashlib.sha256(
        json.dumps(
            {"tool": "upload_interview_result", "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    effect = "read" if dry_run else "write"
    receipt = (
        {
            "result_record_id": "result-1",
            "processing_status": "completed",
        }
        if safe_to_confirm and not dry_run
        else {}
    )
    events = [
        {"type": "session", "id": "xiaoqing-session"},
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "xiaoqing-upload-1",
            "toolName": "upload_interview_result",
            "args": {"arguments": arguments},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "xiaoqing-upload-1",
            "toolName": "upload_interview_result",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": '{"status":"completed"}'}],
                "details": {
                    "protocolVersion": 1,
                    "bridge": "xiaoqing_interview",
                    "effect": effect,
                    "operation": "upload_interview_result",
                    "operationDigest": operation_digest,
                    "targetIdentifiers": {"candidate_id": "candidate-1"},
                    "resultDigest": "c" * 64,
                    "exitCode": 0,
                    "completed": True,
                    "safeToConfirm": safe_to_confirm and not dry_run,
                    "receipt": receipt,
                },
            },
        },
        json.loads(_pi_result_event()),
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    ]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _pi_lark_write_jsonl(*, safe_to_confirm: bool = True) -> str:
    argv = [
        "lark-cli",
        "im",
        "messages",
        "create",
        "--receive-id",
        "chat-1",
        "--json",
    ]
    operation_digest = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    receipt = (
        {
            "resultIdentifiers": {"message_id": "message-1"},
            "processingStatus": "completed",
        }
        if safe_to_confirm
        else {}
    )
    events = [
        {"type": "session", "id": "lark-session"},
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "lark-write-1",
            "toolName": "execute_reviewed_lark_write",
            "args": {"argv": argv},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "lark-write-1",
            "toolName": "execute_reviewed_lark_write",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": '{"message_id":"message-1"}'}],
                "details": {
                    "protocolVersion": 1,
                    "cli": "lark-cli",
                    "effect": "write",
                    "operation": "im messages create",
                    "operationDigest": operation_digest,
                    "targetIdentifiers": {"receive-id": "chat-1"},
                    "resultDigest": "d" * 64,
                    "exitCode": 0,
                    "completed": True,
                    "safeToConfirm": safe_to_confirm,
                    "receipt": receipt,
                },
            },
        },
        json.loads(_pi_result_event()),
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    ]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _pi_lark_reconciliation_jsonl() -> str:
    argv = [
        "lark-cli",
        "im",
        "messages",
        "list",
        "--receive-id",
        "chat-1",
        "--json",
    ]
    operation_digest = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result_text = json.dumps({"items": [{"message_id": "message-1"}]})
    events = [
        {"type": "session", "id": "lark-reconciliation-session"},
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "lark-read-1",
            "toolName": "execute_reviewed_lark_read",
            "args": {"argv": argv},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "lark-read-1",
            "toolName": "execute_reviewed_lark_read",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": result_text}],
                "details": {
                    "protocolVersion": 1,
                    "cli": "lark-cli",
                    "effect": "read",
                    "operation": "im messages list",
                    "operationDigest": operation_digest,
                    "targetIdentifiers": {"receive-id": "chat-1"},
                    "resultDigest": hashlib.sha256(result_text.encode()).hexdigest(),
                    "exitCode": 0,
                    "completed": True,
                    "safeToConfirm": False,
                    "receipt": {},
                },
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "outcome": "completed",
                                "summary": "Lark live read confirmed the message.",
                                "proof": {"observed_state": "effect_present"},
                                "error": {
                                    "code": "",
                                    "retryable": False,
                                    "authorization_required": False,
                                },
                            }
                        ),
                    }
                ],
            },
        },
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    ]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


class RecordingExecutor:
    def __init__(
        self,
        output: str,
        *,
        returncode: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.output = output
        self.returncode = returncode
        self.timed_out = timed_out
        self.commands: list[list[str]] = []
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, command, *, prompt, on_stdout_line, **kwargs):
        self.commands.append(command)
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        for line in self.output.splitlines():
            on_stdout_line(line)
        return ProcessRunResult(
            returncode=self.returncode,
            stdout=self.output,
            stderr="process failed" if self.returncode else "",
            timed_out=self.timed_out,
            timeout_kind="total" if self.timed_out else "",
            timeout_reason="process timed out" if self.timed_out else "",
        )


@pytest.fixture
def store(tmp_path: Path) -> AutoReplyStore:
    return AutoReplyStore(tmp_path / "reply.sqlite3")


def test_direct_runner_uses_isolated_pi_configuration(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(_jsonl())

    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
    ).run(task, _context(task.id))

    command = executor.commands[0]
    command_text = " ".join(command)
    assert command[1].endswith("/pi/packages/coding-agent/dist/cli.js")
    assert command[command.index("--mode") + 1] == "json"
    assert "--session-id" not in command
    assert "--offline" in command
    assert "--no-context-files" in command
    assert "enabled_tools=" not in command_text
    assert "mcp_servers.brightdata.enabled=false" not in command
    assert "mcp_servers.crm_connector.enabled=false" not in command
    assert "mcp_servers.fundflow.enabled=false" not in command
    assert "features.plugins=false" not in command_text
    assert "features.apps=false" not in command_text
    assert "reconciliation_cli" not in command_text
    assert "--approve" in command
    assert "--output-schema" not in command
    assert str(AGENT_RESULT_SCHEMA_PATH) not in command
    assert result.result.outcome is AgentOutcome.COMPLETED
    assert result.events == ()
    assert result.receipts == ()


def test_direct_runner_persists_confirmed_pi_dws_write_and_receipt(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(
        _pi_tool_event_jsonl(command="dws chat message send --conversation cid")
    )
    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
    ).run(task, _context(task.id))

    run = store.get_agent_run(result.run_id)
    assert run is not None and run.status == "completed"
    assert run.side_effect_state == "confirmed"
    assert [event["type"] for event in result.events] == [
        "item.started",
        "item.completed",
    ]
    assert result.events[0]["item"]["metadata"]["operation"] == "chat message send"
    assert len(result.receipts) == 1
    assert result.receipts[0].operation_id == "call-1"
    assert result.receipts[0].operation == "chat message send"
    assert result.receipts[0].target_identifiers == {"conversation": "cid"}


def test_direct_runner_requires_reviewed_pi_write_confirmation_details(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    raw = _pi_tool_event_jsonl(
        command="dws chat message send --conversation cid"
    )
    payloads = [json.loads(line) for line in raw.splitlines()]
    for payload in payloads:
        if payload.get("type") == "tool_execution_end":
            payload["result"].pop("details")
    executor = RecordingExecutor("\n".join(json.dumps(item) for item in payloads))
    with pytest.raises(AgentRunUnknownError, match="pi_unreviewed_tool_effect"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=executor,
        ).run(task, _context(task.id))

    run = store.get_agent_run_for_task_generation(task.id, task.execution_generation)
    assert run is not None and run.side_effect_state == "unknown"
    assert store.list_agent_execution_receipts(run.id) == []


def test_direct_runner_rejects_mismatched_pi_write_confirmation_digest(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    raw = _pi_tool_event_jsonl(
        command="dws chat message send --conversation cid"
    )
    payloads = [json.loads(line) for line in raw.splitlines()]
    for payload in payloads:
        if payload.get("type") == "tool_execution_end":
            payload["result"]["details"]["operationDigest"] = "wrong"
    executor = RecordingExecutor("\n".join(json.dumps(item) for item in payloads))
    with pytest.raises(AgentRunUnknownError, match="pi_unreviewed_tool_effect"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=executor,
        ).run(task, _context(task.id))

    run = store.get_agent_run_for_task_generation(task.id, task.execution_generation)
    assert run is not None and run.side_effect_state == "unknown"
    assert store.list_agent_execution_receipts(run.id) == []


def test_direct_runner_persists_confirmed_memory_write_receipt(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(_pi_memory_write_jsonl())

    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
    ).run(task, _context(task.id))

    run = store.get_agent_run(result.run_id)
    assert run is not None and run.side_effect_state == "confirmed"
    assert len(result.receipts) == 1
    receipt = result.receipts[0]
    assert receipt.operation_id == "memory-write-1"
    assert receipt.cli == "memory_connector"
    assert receipt.operation == "memory_write"


def test_direct_runner_keeps_unconfirmed_memory_write_unknown(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(
        _pi_memory_write_jsonl(safe_to_confirm=False)
    )

    with pytest.raises(AgentRunUnknownError, match="pi_unreviewed_tool_effect"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=executor,
        ).run(task, _context(task.id))

    run = store.get_agent_run_for_task_generation(task.id, task.execution_generation)
    assert run is not None and run.side_effect_state == "unknown"
    assert store.list_agent_execution_receipts(run.id) == []


def test_direct_runner_confirms_document_upload_only_after_processing_completed(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_pi_document_upload_jsonl()),
    ).run(task, _context(task.id))

    run = store.get_agent_run(result.run_id)
    assert run is not None and run.side_effect_state == "confirmed"
    assert len(result.receipts) == 1
    assert result.receipts[0].operation == "document_upload"


def test_direct_runner_keeps_document_upload_without_terminal_status_unknown(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)

    with pytest.raises(AgentRunUnknownError, match="pi_unreviewed_tool_effect"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=RecordingExecutor(
                _pi_document_upload_jsonl(include_processing_status=False)
            ),
        ).run(task, _context(task.id))

    run = store.get_agent_run_for_task_generation(task.id, task.execution_generation)
    assert run is not None and run.side_effect_state == "unknown"
    assert store.list_agent_execution_receipts(run.id) == []


def test_direct_runner_persists_confirmed_xiaoqing_upload_receipt(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(_pi_xiaoqing_upload_jsonl())

    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
    ).run(task, _context(task.id))

    run = store.get_agent_run(result.run_id)
    assert run is not None and run.side_effect_state == "confirmed"
    assert len(result.receipts) == 1
    receipt = result.receipts[0]
    assert receipt.operation_id == "xiaoqing-upload-1"
    assert receipt.cli == "xiaoqing_interview"
    assert receipt.operation == "upload_interview_result"
    assert receipt.target_identifiers == {"candidate_id": "candidate-1"}


def test_direct_runner_keeps_unconfirmed_xiaoqing_upload_unknown(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(
        _pi_xiaoqing_upload_jsonl(safe_to_confirm=False)
    )

    with pytest.raises(AgentRunUnknownError, match="pi_unreviewed_tool_effect"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=executor,
        ).run(task, _context(task.id))


def test_direct_runner_treats_xiaoqing_dry_run_as_read_only(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_pi_xiaoqing_upload_jsonl(dry_run=True)),
    ).run(task, _context(task.id))

    run = store.get_agent_run(result.run_id)
    assert run is not None and run.side_effect_state == "none"
    assert result.receipts == ()
    assert result.events[0]["item"]["metadata"]["effect"] == "read_only"


def test_direct_runner_persists_confirmed_lark_write_receipt(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_pi_lark_write_jsonl()),
    ).run(task, _context(task.id))

    run = store.get_agent_run(result.run_id)
    assert run is not None and run.side_effect_state == "confirmed"
    assert len(result.receipts) == 1
    receipt = result.receipts[0]
    assert receipt.cli == "lark-cli"
    assert receipt.operation == "im messages create"
    assert receipt.target_identifiers == {"receive-id": "chat-1"}


def test_direct_runner_keeps_unconfirmed_lark_write_unknown(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    with pytest.raises(AgentRunUnknownError, match="pi_unreviewed_tool_effect"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=RecordingExecutor(
                _pi_lark_write_jsonl(safe_to_confirm=False)
            ),
        ).run(task, _context(task.id))


def test_pi_reconciliation_accepts_reviewed_lark_live_read(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    unknown = _unknown_run(
        store,
        task.id,
        native_cli="lark-cli",
        operation="im messages create",
        target_identifiers={"receive-id": "chat-1"},
    )
    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_pi_lark_reconciliation_jsonl()),
    ).reconcile(unknown, _context(task.id))

    assert result.result.outcome is AgentOutcome.COMPLETED
    assert result.result.proof == ReconciliationProof(
        observed_state="effect_present"
    )
    assert result.events[0]["item"]["metadata"]["native_cli"] == "lark-cli"


def test_direct_runner_marks_interrupted_pi_write_unknown_without_retrying(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(
        _pi_tool_event_jsonl(
            command="dws chat message send --conversation cid",
            include_end=False,
        ),
        timed_out=True,
    )
    with pytest.raises(AgentRunUnknownError, match="pi_process_timeout"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=executor,
        ).run(task, _context(task.id))

    run = store.get_agent_run_for_task_generation(
        task.id,
        task.execution_generation,
    )
    assert run is not None and run.status == "unknown"
    assert run.side_effect_state == "unknown"
    assert run.tool_events[0]["type"] == "item.started"


def test_direct_runner_treats_failed_pi_write_as_outcome_unknown(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(
        _pi_tool_event_jsonl(
            command="dws chat message send --conversation cid",
            tool_error=True,
        )
    )
    with pytest.raises(AgentRunUnknownError, match="pi_unreviewed_tool_effect"):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=executor,
        ).run(task, _context(task.id))

    run = store.get_agent_run_for_task_generation(
        task.id,
        task.execution_generation,
    )
    assert run is not None and run.status == "unknown"
    assert run.side_effect_state == "unknown"


def test_direct_agent_requires_oa_applicant_notification_after_confirmed_action():
    instructions = direct_agent_developer_instructions()

    assert "outcome, summary, and error" in instructions
    assert "completed, no_action, needs_human, or failed" in instructions
    assert "error is always an object" in instructions
    assert "oa_action_receipt.result" in instructions
    assert "notify that applicant through DingTalk before returning AgentResult" in instructions
    assert "real originator identifier" in instructions
    assert "does not approve, reject, or return the approval" in instructions
    assert "reviewed Memory tools" in instructions
    assert "Exa is read-only" in instructions
    assert "graphify_read" in instructions
    assert "returns the image pixels directly to this turn" in instructions
    assert "download_dingtalk_image" in instructions
    assert "Xiaoqing exposes five reads" in instructions
    assert "official lark-cli risk metadata" in instructions


def test_pi_reconciliation_uses_only_reviewed_read_and_binds_live_proof(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    unknown = _unknown_run(store, task.id)
    executor = RecordingExecutor(_pi_reconciliation_jsonl())
    runner = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
    )

    result = runner.reconcile(unknown, _context(task.id))

    assert result.result.outcome is AgentOutcome.COMPLETED
    assert result.result.proof == ReconciliationProof(
        observed_state="effect_present"
    )
    assert len(result.events) == 2
    command = executor.commands[0]
    tools = command[command.index("--tools") + 1]
    assert tools == (
        "workspace_read,workspace_search,workspace_list,graphify_read,download_dingtalk_image,execute_reviewed_read,"
        "execute_reviewed_lark_read,"
        "user_get,memory_recall,memory_get,timeline_get,web_search_exa,web_fetch_exa,"
        "search_candidates,get_dashboard_stats,get_interview_context,"
        "download_attachment,list_candidate_interviews"
    )
    assert "execute_reviewed_write" not in tools
    after = store.get_agent_run(unknown.id)
    assert after is not None and after.status == "unknown"
    assert after.lease_owner == runner.owner


def test_pi_reconciliation_cannot_confirm_without_matching_live_read(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    unknown = _unknown_run(store, task.id)
    executor = RecordingExecutor(_pi_reconciliation_jsonl(include_read=False))
    runner = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
    )

    with pytest.raises(RuntimeError, match="reconciliation_proof_invalid"):
        runner.reconcile(unknown, _context(task.id))

    after = store.get_agent_run(unknown.id)
    assert after is not None and after.status == "unknown"


def test_direct_runner_reuses_one_codex_session_for_the_conversation(
    tmp_path: Path,
    store: AutoReplyStore,
):
    store.upsert_conversation("cid", "产品群", False, "conversation-session")
    task = _task(store)
    executor = RecordingExecutor(_jsonl(session_id="conversation-session"))
    DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
            session_exists=lambda _session_id: True,
    ).run(task, _context(task.id))

    assert executor.commands[0][executor.commands[0].index("--session-id") + 1] == (
        "conversation-session"
    )
    run = store.get_agent_run_for_task_generation(
        task.id,
        task.execution_generation,
    )
    assert run.codex_session_id == "conversation-session"


def test_direct_runner_serializes_same_conversation_before_claiming_second_run(
    tmp_path: Path,
    store: AutoReplyStore,
):
    first = _task(store)
    store.enqueue_reply_task(
        channel="dingtalk",
        conversation_id="cid",
        conversation_title="产品群",
        single_chat=False,
        trigger_message_id="mid-2",
        trigger_create_time="2026-07-28 12:01:00",
        trigger_sender="ET",
        trigger_text="继续处理",
        execution_generation="generation-2",
    )
    second = store.get_reply_task_for_message("cid", "mid-2")
    assert second is not None
    second_context = replace(
        _context(second.id),
        trigger_message_id="mid-2",
        trigger_text="继续处理",
        trigger_create_time="2026-07-28 12:01:00",
    )

    second_runner = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_jsonl(session_id="second-session")),
    )

    class ReentrantExecutor(RecordingExecutor):
        def __call__(self, command, *, prompt, on_stdout_line, **kwargs):
            with pytest.raises(AgentConversationLockedError):
                second_runner.run(second, second_context)
            return super().__call__(
                command,
                prompt=prompt,
                on_stdout_line=on_stdout_line,
                **kwargs,
            )

    DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=ReentrantExecutor(_jsonl(session_id="first-session")),
    ).run(first, _context(first.id))

    assert (
        store.get_agent_run_for_task_generation(
            second.id,
            second.execution_generation,
        )
        is None
    )
    assert store.acquire_codex_session_lock("cid", "post-run") is True


def test_direct_runner_persists_sanitized_process_failure_detail(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(_jsonl(), returncode=1)

    with pytest.raises(RuntimeError, match="pi_process_failed"):
        DirectAgentRunner(store=store, workspace=tmp_path, executor=executor).run(
            task,
            _context(task.id),
        )

    run = store.get_agent_run_for_task_generation(
        task.id,
        task.execution_generation,
    )
    assert run is not None
    assert json.loads(run.structured_error_json)["detail"] == "process failed"


def test_direct_runner_does_not_reinject_unrelated_user_mcp_servers(
    tmp_path: Path,
    store: AutoReplyStore,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "[mcp_servers.unrelated_plugin]\n"
        'url = "https://example.invalid/mcp"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    task = _task(store)
    executor = RecordingExecutor(_jsonl())

    DirectAgentRunner(store=store, workspace=tmp_path, executor=executor).run(
        task,
        _context(task.id),
    )

    command = executor.commands[0]
    assert "mcp_servers.unrelated_plugin.enabled=false" not in command


def test_direct_runner_persists_new_session_for_later_conversation_messages(
    tmp_path: Path,
    store: AutoReplyStore,
):
    first = _task(store)
    DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_jsonl(session_id="conversation-session")),
    ).run(first, _context(first.id))

    store.enqueue_reply_task(
        channel="dingtalk",
        conversation_id="cid",
        conversation_title="产品群",
        single_chat=False,
        trigger_message_id="mid-2",
        trigger_create_time="2026-07-28 12:01:00",
        trigger_sender="ET",
        trigger_text="继续处理",
        execution_generation="generation-2",
    )
    second = store.get_reply_task_for_message("cid", "mid-2")
    second_context = replace(
        _context(second.id),
        trigger_message_id="mid-2",
        trigger_text="继续处理",
        trigger_create_time="2026-07-28 12:01:00",
    )
    executor = RecordingExecutor(_jsonl(session_id="conversation-session"))

    DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
        session_exists=lambda _session_id: True,
    ).run(second, second_context)

    assert store.get_codex_session_id("cid") == "conversation-session"
    assert executor.commands[0][executor.commands[0].index("--session-id") + 1] == (
        "conversation-session"
    )


def test_direct_runner_starts_fresh_when_conversation_session_is_missing(
    tmp_path: Path,
    store: AutoReplyStore,
):
    store.upsert_conversation("cid", "产品群", False, "missing-session")
    task = _task(store)
    executor = RecordingExecutor(_jsonl(session_id="replacement-session"))

    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
        session_exists=lambda _session_id: False,
    ).run(task, _context(task.id))

    assert executor.commands[0][1].endswith("/pi/packages/coding-agent/dist/cli.js")
    assert "--session-id" not in executor.commands[0]
    assert store.get_codex_session_id("cid") == "replacement-session"
    assert store.get_agent_run(result.run_id).codex_session_id == "replacement-session"


def test_direct_runner_only_persists_codex_session_pointer_not_tool_event_copy(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)

    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_jsonl(session_id="native-audit-session")),
    ).run(task, _context(task.id))

    run = store.get_agent_run(result.run_id)
    assert run.codex_session_id == "native-audit-session"
    assert run.tool_events == []
    assert result.transcript_end_line >= result.transcript_start_line + 3


def test_direct_runner_renews_lease_when_stream_reports_progress(
    tmp_path: Path,
    store: AutoReplyStore,
    monkeypatch: pytest.MonkeyPatch,
):
    claimed_at = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
    progress_at = datetime(2026, 7, 29, 0, 35, tzinfo=timezone.utc)
    times = iter((claimed_at, *([progress_at] * 12)))

    def controlled_store_time(_now=None):
        value = next(times, progress_at)
        return value, value.strftime("%Y-%m-%d %H:%M:%S")

    monkeypatch.setattr("app.store._utc_store_time", controlled_store_time)
    task = _task(store)
    output = _jsonl(session_id="progress-session")
    observed_leases: list[str] = []

    def executor(command, *, prompt, on_stdout_line, **_kwargs):
        for line in output.splitlines():
            on_stdout_line(line)
        run = store.get_agent_run_for_task_generation(
            task.id,
            task.execution_generation,
        )
        assert run is not None
        observed_leases.append(run.lease_expires_at)
        return ProcessRunResult(returncode=0, stdout=output, stderr="")

    DirectAgentRunner(store=store, workspace=tmp_path, executor=executor).run(
        task,
        _context(task.id),
    )

    assert observed_leases == ["2026-07-29 01:15:00"]


def test_read_only_run_uses_native_tools_with_never_approval_policy(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)
    executor = RecordingExecutor(_jsonl())

    DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=executor,
    ).run(task, _context(task.id), read_only=True)

    command_text = " ".join(executor.commands[0])
    assert executor.commands[0][executor.commands[0].index("--tools") + 1] == (
        "workspace_read,workspace_search,workspace_list,graphify_read,download_dingtalk_image,execute_reviewed_read,"
        "execute_reviewed_lark_read,"
        "user_get,memory_recall,memory_get,timeline_get,web_search_exa,web_fetch_exa,"
        "search_candidates,get_dashboard_stats,get_interview_context,"
        "download_attachment,list_candidate_interviews"
    )
    assert "reconciliation_cli" not in command_text
    assert "enabled_tools=" not in command_text
    assert "Read-only invocation" in executor.prompts[0]


def test_structured_failed_result_is_returned_and_persisted(
    tmp_path: Path,
    store: AutoReplyStore,
):
    task = _task(store)

    result = DirectAgentRunner(
        store=store,
        workspace=tmp_path,
        executor=RecordingExecutor(_jsonl(outcome="failed")),
    ).run(task, _context(task.id))

    assert result.result.outcome is AgentOutcome.FAILED
    run = store.get_agent_run(result.run_id)
    assert run.status == "failed"
    assert "material_unavailable" in run.structured_error_json


@pytest.mark.parametrize(
    ("executor", "error_code"),
    [
        (RecordingExecutor(_jsonl(), returncode=1), "pi_process_failed"),
        (RecordingExecutor(_jsonl(), timed_out=True), "pi_process_timeout"),
        (RecordingExecutor("not-json"), "pi_result_invalid"),
    ],
)
def test_runtime_failures_are_persisted_as_regular_failures(
    tmp_path: Path,
    store: AutoReplyStore,
    executor: RecordingExecutor,
    error_code: str,
):
    task = _task(store)

    with pytest.raises(RuntimeError, match=error_code):
        DirectAgentRunner(
            store=store,
            workspace=tmp_path,
            executor=executor,
        ).run(task, _context(task.id))

    run = store.get_agent_run_for_task_generation(
        task.id,
        task.execution_generation,
    )
    assert run.status == "failed"
    assert run.side_effect_state == "none"
    assert error_code in run.structured_error_json
