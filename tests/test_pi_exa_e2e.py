from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

from app.pi_events import summarize_pi_stream
from app.pi_runner import PiRunner, pi_cli_path, pi_process_failure_reason
from app.pi_safety import set_pi_tools


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _fake_exa_server():
    port = _free_port()
    calls: list[dict[str, object]] = []
    headers: list[dict[str, str]] = []
    exa = FastMCP(
        "pi-e2e-exa",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        log_level="ERROR",
    )

    @exa.tool()
    def web_search_exa(query: str, numResults: int = 5) -> dict[str, object]:
        calls.append({"query": query, "numResults": numResults})
        return {
            "results": [
                {
                    "title": "Reviewed Exa result",
                    "url": "https://example.com/reviewed-exa",
                }
            ]
        }

    app = exa.streamable_http_app()

    async def capture_headers(request, call_next):
        headers.append(dict(request.headers))
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=capture_headers)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}/mcp", calls, headers
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@contextmanager
def _fake_responses_provider():
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
            events = _final_response_events() if has_tool_result else _exa_call_events()
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


def _exa_call_events() -> list[dict[str, object]]:
    arguments = json.dumps(
        {"query": "reviewed Pi Exa bridge", "numResults": 3},
        separators=(",", ":"),
    )
    added_item = {
        "type": "function_call",
        "id": "fc_exa_e2e_1",
        "call_id": "call_exa_e2e_1",
        "name": "web_search_exa",
        "arguments": "",
        "status": "in_progress",
    }
    completed_item = {**added_item, "arguments": arguments, "status": "completed"}
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_exa_tool", "status": "in_progress"},
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
            "item_id": "fc_exa_e2e_1",
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
                "id": "resp_exa_tool",
                "status": "completed",
                "output": [completed_item],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        },
    ]


def _final_response_events() -> list[dict[str, object]]:
    text = '{"status":"searched"}'
    added_item = {
        "type": "message",
        "id": "msg_exa_e2e_2",
        "role": "assistant",
        "status": "in_progress",
        "content": [],
    }
    completed_item = {
        **added_item,
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_exa_final", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": added_item,
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 2,
            "output_index": 0,
            "content_index": 0,
            "item_id": "msg_exa_e2e_2",
            "delta": text,
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
                "id": "resp_exa_final",
                "status": "completed",
                "output": [completed_item],
                "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            },
        },
    ]


def test_real_pi_exa_e2e_uses_fake_provider_and_fake_mcp(tmp_path, monkeypatch):
    assert pi_cli_path().is_file(), "sibling Pi CLI build is required for this test"
    with _fake_exa_server() as (exa_url, exa_calls, exa_headers):
        with _fake_responses_provider() as (provider_url, provider_requests):
            monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-codex-home"))
            monkeypatch.setenv("CEO_PI_PROVIDER", "pi-e2e-provider")
            monkeypatch.setenv("CEO_PI_MODEL", "pi-e2e-model")
            monkeypatch.setenv("CEO_PI_MODEL_SOURCE", "custom")
            monkeypatch.setenv("CEO_PI_API", "openai-responses")
            monkeypatch.setenv("CEO_PI_BASE_URL", provider_url)
            monkeypatch.setenv("CEO_PI_API_KEY", "fake-provider-secret")
            monkeypatch.setenv("CEO_PI_THINKING_LEVEL", "off")
            monkeypatch.setenv("CEO_PI_AGENT_DIR", str(tmp_path / "pi-agent"))
            monkeypatch.setenv("CEO_PI_SESSION_DIR", str(tmp_path / "pi-sessions"))
            monkeypatch.setenv("CEO_PI_PYTHON_BINARY", sys.executable)
            monkeypatch.setenv("CEO_PI_EXA_MCP_URL", exa_url)

            prompt = "Search Exa exactly once for the supplied query."
            runner = PiRunner(workspace=tmp_path)
            command = runner.build_command(
                prompt,
                session_id=None,
                use_output_schema=False,
                approval_policy="never",
                developer_instructions="Use only web_search_exa, then return JSON.",
            )
            set_pi_tools(command, ("web_search_exa",))
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
    summary = summarize_pi_stream(completed.stdout)
    assert summary.saw_agent_start is True
    assert summary.saw_agent_end is True
    assert summary.saw_agent_settled is True
    assert summary.incomplete_tool_call_ids == ()
    assert exa_calls == [{"query": "reviewed Pi Exa bridge", "numResults": 3}]
    assert len(provider_requests) == 2
    assert all(request["path"] == "/v1/responses" for request in provider_requests)
    assert "fake-provider-secret" not in json.dumps(exa_headers)
    assert "fake-provider-secret" not in json.dumps(exa_calls)
    assert any(
        str(
            {str(key).lower(): value for key, value in request["headers"].items()}.get(
                "authorization", ""
            )
        )
        == "Bearer fake-provider-secret"
        for request in provider_requests
    )
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    starts = [event for event in events if event.get("type") == "tool_execution_start"]
    ends = [event for event in events if event.get("type") == "tool_execution_end"]
    assert len(starts) == len(ends) == 1
    assert starts[0]["toolName"] == "web_search_exa"
    details = ends[0]["result"]["details"]
    assert details["bridge"] == "exa"
    assert details["effect"] == "read"
    assert details["completed"] is True
    assert details["safeToConfirm"] is False
