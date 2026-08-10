from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

from app.pi_xiaoqing_bridge import (
    PiXiaoqingBridgeError,
    _confirmed_upload_receipt,
    call_xiaoqing_tool,
    validate_xiaoqing_arguments,
    xiaoqing_headers,
    xiaoqing_mcp_url,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _fake_xiaoqing_server():
    port = _free_port()
    calls: list[tuple[str, dict[str, object]]] = []
    headers: list[dict[str, str]] = []
    server_api = FastMCP(
        "pi-xiaoqing-test",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        log_level="ERROR",
    )

    @server_api.tool()
    def search_candidates(query: str, limit: int = 10) -> dict[str, object]:
        calls.append(("search_candidates", {"query": query, "limit": limit}))
        return {"candidates": [{"candidate_id": "candidate-1"}]}

    @server_api.tool()
    def upload_interview_result(
        candidate_id: str,
        summary: str,
        dry_run: bool = False,
    ) -> dict[str, object]:
        calls.append(
            (
                "upload_interview_result",
                {
                    "candidate_id": candidate_id,
                    "summary": summary,
                    "dry_run": dry_run,
                },
            )
        )
        if dry_run:
            return {"status": "validated", "candidate_id": candidate_id}
        return {
            "status": "completed",
            "interview_result_id": "result-1",
            "candidate_id": candidate_id,
        }

    app = server_api.streamable_http_app()

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


def test_xiaoqing_url_and_access_token_are_fail_closed():
    assert xiaoqing_mcp_url({}) == "https://interview.hr.startask.net/mcp"
    assert xiaoqing_mcp_url(
        {"CEO_PI_XIAOQING_MCP_URL": "http://127.0.0.1:9999/mcp/"}
    ) == "http://127.0.0.1:9999/mcp"
    with pytest.raises(PiXiaoqingBridgeError, match="xiaoqing_mcp_url_insecure"):
        xiaoqing_mcp_url(
            {"CEO_PI_XIAOQING_MCP_URL": "http://xiaoqing.example/mcp"}
        )
    assert xiaoqing_headers({}) == {}
    assert xiaoqing_headers(
        {"CEO_PI_XIAOQING_ACCESS_TOKEN": "local-oauth-token"}
    ) == {"Authorization": "Bearer local-oauth-token"}
    with pytest.raises(PiXiaoqingBridgeError, match="access_token_invalid"):
        xiaoqing_headers(
            {"CEO_PI_XIAOQING_ACCESS_TOKEN": "Bearer must-not-double-prefix"}
        )


def test_xiaoqing_argument_policy_rejects_unknown_tools_and_bad_dry_run():
    with pytest.raises(PiXiaoqingBridgeError, match="tool_not_allowed"):
        validate_xiaoqing_arguments("delete_candidate", {})
    with pytest.raises(PiXiaoqingBridgeError, match="dry_run_not_supported"):
        validate_xiaoqing_arguments("search_candidates", {"dry_run": True})
    with pytest.raises(PiXiaoqingBridgeError, match="dry_run_invalid"):
        validate_xiaoqing_arguments(
            "upload_interview_result",
            {"dry_run": "true"},
        )


def test_xiaoqing_bridge_calls_read_and_write_with_receipt(monkeypatch):
    with _fake_xiaoqing_server() as (url, calls, headers):
        monkeypatch.setenv("CEO_PI_XIAOQING_MCP_URL", url)
        monkeypatch.setenv("CEO_PI_XIAOQING_ACCESS_TOKEN", "xiaoqing-secret")
        read = asyncio.run(
            call_xiaoqing_tool(
                "search_candidates",
                {"query": "platform engineer", "limit": 3},
            )
        )
        dry_run = asyncio.run(
            call_xiaoqing_tool(
                "upload_interview_result",
                {
                    "candidate_id": "candidate-1",
                    "summary": "reviewed",
                    "dry_run": True,
                },
            )
        )
        write = asyncio.run(
            call_xiaoqing_tool(
                "upload_interview_result",
                {
                    "candidate_id": "candidate-1",
                    "summary": "reviewed",
                    "dry_run": False,
                },
            )
        )

    assert read["effect"] == "read"
    assert read["confirmed"] is False
    assert dry_run["effect"] == "read"
    assert dry_run["confirmed"] is False
    assert dry_run["receipt"] == {}
    assert write["effect"] == "write"
    assert write["confirmed"] is True
    assert write["receipt"] == {
        "result_record_id": "result-1",
        "processing_status": "completed",
    }
    assert calls == [
        ("search_candidates", {"query": "platform engineer", "limit": 3}),
        (
            "upload_interview_result",
            {
                "candidate_id": "candidate-1",
                "summary": "reviewed",
                "dry_run": True,
            },
        ),
        (
            "upload_interview_result",
            {
                "candidate_id": "candidate-1",
                "summary": "reviewed",
                "dry_run": False,
            },
        ),
    ]
    assert any(
        item.get("authorization") == "Bearer xiaoqing-secret" for item in headers
    )


def test_xiaoqing_upload_receipt_requires_one_completed_matching_record():
    arguments = {"candidate_id": "candidate-1", "summary": "reviewed"}
    payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "status": "completed",
                        "interview_result_id": "result-1",
                        "candidate_id": "candidate-1",
                    }
                ),
            }
        ]
    }

    assert _confirmed_upload_receipt(payload, arguments) == {
        "result_record_id": "result-1",
        "processing_status": "completed",
    }
    assert _confirmed_upload_receipt(
        {
            "records": [
                {
                    "interview_result_id": "result-from-one-record",
                    "candidate_id": "candidate-1",
                },
                {"status": "completed", "candidate_id": "candidate-1"},
            ]
        },
        arguments,
    ) == {}
    assert _confirmed_upload_receipt(
        {
            "status": "completed",
            "interview_result_id": "wrong-candidate-result",
            "candidate_id": "candidate-2",
        },
        arguments,
    ) == {}


def test_xiaoqing_bridge_enforces_live_schema(monkeypatch):
    with _fake_xiaoqing_server() as (url, _calls, _headers):
        monkeypatch.setenv("CEO_PI_XIAOQING_MCP_URL", url)
        with pytest.raises(
            PiXiaoqingBridgeError,
            match="xiaoqing_required_argument_missing",
        ):
            asyncio.run(call_xiaoqing_tool("search_candidates", {"limit": 1}))


def test_xiaoqing_bridge_subprocess_never_echoes_access_token(monkeypatch):
    with _fake_xiaoqing_server() as (url, _calls, _headers):
        process_env = dict(os.environ)
        process_env["CEO_PI_XIAOQING_MCP_URL"] = url
        process_env["CEO_PI_XIAOQING_ACCESS_TOKEN"] = "subprocess-secret"
        completed = subprocess.run(
            [sys.executable, "app/pi_xiaoqing_bridge.py"],
            input=json.dumps(
                {
                    "tool": "search_candidates",
                    "arguments": {"query": "security", "limit": 1},
                }
            ),
            text=True,
            capture_output=True,
            env=process_env,
            timeout=30,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    assert "subprocess-secret" not in completed.stdout
    assert "subprocess-secret" not in completed.stderr
    assert json.loads(completed.stdout)["tool"] == "search_candidates"
