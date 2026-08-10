from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.pi_events import summarize_pi_stream
from app.pi_runner import PiRunner, pi_cli_path, pi_process_failure_reason


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _fake_responses_provider(markdown: str):
    port = _free_port()
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "payload": payload,
                }
            )
            input_items = payload.get("input") if isinstance(payload, dict) else []
            has_tool_result = any(
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                for item in input_items or []
            )
            events = (
                _final_response_events()
                if has_tool_result
                else _profile_write_call_events(markdown)
            )
            body = "".join(
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                for event in events
            ) + "data: [DONE]\n\n"
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _profile_write_call_events(markdown: str) -> list[dict[str, object]]:
    arguments = json.dumps({"markdown": markdown}, separators=(",", ":"))
    added_item = {
        "type": "function_call",
        "id": "fc_profile_e2e_1",
        "call_id": "call_profile_e2e_1",
        "name": "write_work_profile",
        "arguments": "",
        "status": "in_progress",
    }
    completed_item = {**added_item, "arguments": arguments, "status": "completed"}
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_profile_tool", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": added_item,
        },
        {
            "type": "response.function_call_arguments.done",
            "sequence_number": 2,
            "output_index": 0,
            "item_id": "fc_profile_e2e_1",
            "arguments": arguments,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 3,
            "output_index": 0,
            "item": completed_item,
        },
        {
            "type": "response.completed",
            "sequence_number": 4,
            "response": {
                "id": "resp_profile_tool",
                "status": "completed",
                "output": [completed_item],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        },
    ]


def _final_response_events() -> list[dict[str, object]]:
    text = '{"status":"profile_updated"}'
    item = {
        "type": "message",
        "id": "msg_profile_e2e_2",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_profile_final", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 2,
            "output_index": 0,
            "content_index": 0,
            "item_id": "msg_profile_e2e_2",
            "delta": text,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 3,
            "output_index": 0,
            "item": item,
        },
        {
            "type": "response.completed",
            "sequence_number": 4,
            "response": {
                "id": "resp_profile_final",
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            },
        },
    ]


def test_real_pi_profile_distillation_writes_only_reviewed_profile(
    tmp_path,
    monkeypatch,
):
    assert pi_cli_path().is_file(), "sibling Pi CLI build is required for this test"
    profile = tmp_path / "data" / "work-profile" / "work_profile.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("# Existing profile\n", encoding="utf-8")
    markdown = "# Reviewed profile\n\n- Verify outcomes before reporting completion.\n"

    with _fake_responses_provider(markdown) as (provider_url, provider_requests):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-codex-home"))
        monkeypatch.setenv("CEO_PI_PROVIDER", "pi-e2e-provider")
        monkeypatch.setenv("CEO_PI_MODEL", "pi-e2e-model")
        monkeypatch.setenv("CEO_PI_API", "openai-responses")
        monkeypatch.setenv("CEO_PI_BASE_URL", provider_url)
        monkeypatch.setenv("CEO_PI_API_KEY", "fake-provider-secret")
        monkeypatch.setenv("CEO_PI_THINKING_LEVEL", "off")
        monkeypatch.setenv("CEO_PI_AGENT_DIR", str(tmp_path / "pi-agent"))
        monkeypatch.setenv("CEO_PI_SESSION_DIR", str(tmp_path / "pi-sessions"))
        monkeypatch.setenv("CEO_WORK_PROFILE_PATH", str(profile))

        prompt = "Write the reviewed profile exactly once."
        runner = PiRunner(workspace=tmp_path)
        command = runner.build_command(
            prompt,
            session_id=None,
            use_output_schema=False,
            approval_policy="never",
            developer_instructions=(
                "Use only write_work_profile exactly once, then return JSON."
            ),
            profile_distillation=True,
        )
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            env=runner.build_env(),
            cwd=tmp_path,
            timeout=60,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    assert pi_process_failure_reason(completed.stdout, completed.stderr) == ""
    assert profile.read_text(encoding="utf-8") == markdown
    assert profile.stat().st_mode & 0o777 == 0o600
    summary = summarize_pi_stream(completed.stdout)
    assert summary.saw_agent_start is True
    assert summary.saw_agent_end is True
    assert summary.saw_agent_settled is True
    events = [
        json.loads(line) for line in completed.stdout.splitlines() if line.strip()
    ]
    starts = [
        event
        for event in events
        if event.get("type") == "tool_execution_start"
    ]
    assert [event.get("toolName") for event in starts] == ["write_work_profile"]
    end = next(event for event in events if event.get("type") == "tool_execution_end")
    details = end["result"]["details"]
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    assert details["safeToConfirm"] is True
    assert details["resultDigest"] == digest
    assert details["receipt"] == {"artifact": "work_profile", "sha256": digest}
    assert len(provider_requests) == 2
    assert all(
        "fake-provider-secret" not in json.dumps(request["payload"])
        for request in provider_requests
    )
