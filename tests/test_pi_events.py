import json
from pathlib import Path

import app.pi_history as pi_history
from app.agent_result import AgentOutcome, parse_agent_result
from app.pi_events import (
    assistant_text_candidates,
    pi_session_id_from_payload,
    pi_stream_completion_issue,
    summarize_pi_stream,
)
from app.pi_history import (
    count_pi_session_lines,
    find_pi_session_path,
    render_local_pi_session,
)
from app.task_agent import _parse_task_agent_decision


def _assistant_event(text: str, *, event_type: str = "message_end") -> dict:
    return {
        "type": event_type,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def test_pi_assistant_text_candidates_accept_final_message_events():
    payload = _assistant_event("final answer", event_type="turn_end")

    assert assistant_text_candidates(payload) == ["final answer"]
    assert assistant_text_candidates(
        {
            "type": "message_end",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "not an answer"}],
            },
        }
    ) == []


def test_pi_session_header_extraction():
    assert pi_session_id_from_payload(
        {"type": "session", "version": 3, "id": "session-1"}
    ) == "session-1"
    assert pi_session_id_from_payload({"type": "agent_start", "id": "wrong"}) is None


def test_parse_agent_result_from_pi_message_end():
    result_json = json.dumps(
        {
            "outcome": "completed",
            "summary": "Pi completed the task",
            "error": {
                "code": "",
                "retryable": False,
                "authorization_required": False,
            },
        }
    )

    result = parse_agent_result(json.dumps(_assistant_event(result_json)))

    assert result.outcome is AgentOutcome.COMPLETED
    assert result.summary == "Pi completed the task"


def test_parse_task_agent_decision_from_pi_turn_end():
    decision_json = json.dumps(
        {
            "action": "discard",
            "discard_reason": "没有状态变化",
            "project": None,
            "todo_changes": [],
            "follow_up_drafts": [],
            "follow_up_changes": [],
            "update_summary": "无变化",
            "merge_reason": "",
            "memory_recall_used": False,
            "confidence": 0.8,
        },
        ensure_ascii=False,
    )

    decision = _parse_task_agent_decision(
        json.dumps(_assistant_event(decision_json, event_type="turn_end"))
    )

    assert decision.action == "discard"
    assert decision.discard_reason == "没有状态变化"


def test_pi_session_path_lookup_and_line_count(tmp_path: Path):
    session_path = tmp_path / "2026-08-08_session-1.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session",
                        "version": 3,
                        "id": "session-1",
                        "cwd": "/workspace",
                    }
                ),
                json.dumps({"type": "agent_start"}),
                json.dumps(_assistant_event("done")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert find_pi_session_path("session-1", session_dir=tmp_path) == session_path
    assert count_pi_session_lines("session-1", session_dir=tmp_path) == 3
    assert find_pi_session_path("../session-1", session_dir=tmp_path) is None
    assert count_pi_session_lines("missing", session_dir=tmp_path) == 0


def test_pi_session_path_lookup_does_not_open_unrelated_sessions(
    tmp_path: Path,
    monkeypatch,
):
    for index in range(20):
        (tmp_path / f"2026-08-08_unrelated-{index}.jsonl").write_text(
            "not-json\n",
            encoding="utf-8",
        )
    inspected: list[Path] = []

    def session_file_id(path: Path) -> str | None:
        inspected.append(path)
        return "session-target"

    monkeypatch.setattr(pi_history, "_pi_session_file_id", session_file_id)

    assert find_pi_session_path("session-target", session_dir=tmp_path) is None
    assert inspected == []

    target = tmp_path / "2026-08-08_session-target.jsonl"
    target.write_text("placeholder\n", encoding="utf-8")
    assert find_pi_session_path("session-target", session_dir=tmp_path) == target
    assert inspected == [target]


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_pi_stream_summary_accepts_settled_lifecycle():
    raw = _stream(
        {"type": "agent_start"},
        _assistant_event("done"),
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    )

    summary = summarize_pi_stream(raw)

    assert summary.saw_lifecycle is True
    assert summary.saw_agent_start is True
    assert summary.saw_agent_end is True
    assert summary.saw_agent_settled is True
    assert pi_stream_completion_issue(raw) == ""


def test_pi_stream_requires_settled_after_real_lifecycle():
    raw = _stream(
        {"type": "agent_start"},
        {"type": "agent_end", "willRetry": False},
    )

    assert pi_stream_completion_issue(raw) == "pi_not_settled"


def test_pi_stream_accepts_completed_retry_and_compaction():
    raw = _stream(
        {"type": "agent_start"},
        {"type": "auto_retry_start", "attempt": 1},
        {"type": "auto_retry_end", "attempt": 1, "success": True},
        {"type": "compaction_start", "reason": "threshold"},
        {
            "type": "compaction_end",
            "reason": "threshold",
            "aborted": False,
            "willRetry": False,
        },
        {"type": "agent_settled"},
    )

    summary = summarize_pi_stream(raw)

    assert summary.retry_starts == summary.retry_ends == 1
    assert summary.compaction_starts == summary.compaction_ends == 1
    assert pi_stream_completion_issue(raw) == ""


def test_pi_stream_rejects_incomplete_retry():
    raw = _stream(
        {"type": "agent_start"},
        {"type": "auto_retry_start", "attempt": 1},
        {"type": "agent_settled"},
    )

    assert pi_stream_completion_issue(raw) == "pi_retry_incomplete"


def test_pi_stream_rejects_incomplete_compaction():
    raw = _stream(
        {"type": "agent_start"},
        {"type": "compaction_start", "reason": "threshold"},
        {"type": "agent_settled"},
    )

    assert pi_stream_completion_issue(raw) == "pi_compaction_incomplete"


def test_pi_stream_rejects_extension_error():
    raw = _stream(
        {"type": "agent_start"},
        {"type": "extension_error", "error": "extension exploded"},
        {"type": "agent_settled"},
    )

    summary = summarize_pi_stream(raw)

    assert summary.extension_errors == ("extension exploded",)
    assert pi_stream_completion_issue(raw) == "pi_extension_failed"


def test_pi_stream_rejects_tool_start_without_end():
    raw = _stream(
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "execute_reviewed_write",
        },
        {"type": "agent_settled"},
    )

    summary = summarize_pi_stream(raw)

    assert summary.incomplete_tool_call_ids == ("call-1",)
    assert pi_stream_completion_issue(raw) == "pi_tool_incomplete"


def test_pi_stream_rejects_malformed_json_after_stream_start():
    raw = _stream({"type": "agent_start"}) + "\nnot-json\n" + _stream(
        {"type": "agent_settled"}
    )

    assert summarize_pi_stream(raw).stream_invalid is True
    assert pi_stream_completion_issue(raw) == "pi_stream_invalid"


def test_pi_stream_keeps_legacy_synthetic_final_event_compatible():
    raw = _stream(_assistant_event("done"))

    assert summarize_pi_stream(raw).saw_lifecycle is False
    assert pi_stream_completion_issue(raw) == ""


def test_pi_history_renders_lifecycle_retry_compaction_and_tool_events(tmp_path: Path):
    session_path = tmp_path / "2026-08-08_session-events.jsonl"
    session_path.write_text(
        _stream(
            {"type": "session", "version": 3, "id": "session-events"},
            {"type": "agent_start"},
            {
                "type": "auto_retry_start",
                "attempt": 1,
                "maxAttempts": 3,
                "delayMs": 100,
                "errorMessage": "temporary",
            },
            {"type": "auto_retry_end", "attempt": 1, "success": True},
            {"type": "compaction_start", "reason": "threshold"},
            {
                "type": "compaction_end",
                "reason": "threshold",
                "aborted": False,
                "willRetry": False,
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "call-1",
                "toolName": "workspace_read",
                "args": {"path": "README.md"},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-1",
                "toolName": "workspace_read",
                "isError": False,
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        )
        + "\n",
        encoding="utf-8",
    )

    rendered = render_local_pi_session("session-events", session_dir=tmp_path)

    assert [event.title for event in rendered.events] == [
        "Pi session metadata",
        "Agent started",
        "Provider retry started",
        "Provider retry ended",
        "Context compaction started",
        "Context compaction ended",
        "Tool started: workspace_read",
        "Tool ended: workspace_read",
        "Agent turn ended",
        "Agent settled",
    ]
