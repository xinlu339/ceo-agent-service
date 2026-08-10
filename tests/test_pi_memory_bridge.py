from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

from app.pi_memory_bridge import (
    PiMemoryBridgeError,
    _memory_call_arguments,
    _write_confirmation,
    call_memory_tool,
    memory_connector_headers,
    validate_memory_arguments,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _memory_server():
    port = _free_port()
    captured_headers: list[dict[str, str]] = []
    memory = FastMCP(
        "test-memory",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        log_level="ERROR",
    )

    @memory.tool()
    def memory_recall(query: str) -> dict[str, object]:
        return {"query": query, "memories": [{"uuid": "memory-1"}]}

    @memory.tool()
    def memory_write(data: str, type: str, created_at: str) -> dict[str, object]:
        return {
            "ok": True,
            "episode_uuid": "episode-1",
            "processing_status": "completed",
            "echo": {"data": data, "type": type, "created_at": created_at},
        }

    app = memory.streamable_http_app()

    async def capture_headers(request, call_next):
        captured_headers.append(dict(request.headers))
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=capture_headers)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}/mcp", captured_headers
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_memory_argument_contract_rejects_acl_scope_and_extra_fields():
    with pytest.raises(PiMemoryBridgeError, match="memory_acl_argument_forbidden"):
        validate_memory_arguments(
            "memory_recall",
            {"query": "project history", "user_id": "invented"},
        )
    with pytest.raises(PiMemoryBridgeError, match="memory_arguments_extra_fields"):
        validate_memory_arguments(
            "memory_write",
            {
                "data": "decision",
                "type": "text",
                "created_at": "2026-08-08T00:00:00Z",
                "source_description": "not allowed",
            },
        )


def test_memory_document_upload_validates_filename_base64_and_ingest_mode():
    with pytest.raises(PiMemoryBridgeError, match="memory_filename_invalid"):
        validate_memory_arguments(
            "document_upload",
            {"filename": "../secret.txt", "content_base64": "eA=="},
        )
    with pytest.raises(PiMemoryBridgeError, match="memory_document_base64_invalid"):
        validate_memory_arguments(
            "document_upload",
            {"filename": "note.txt", "content_base64": "not-base64"},
        )
    with pytest.raises(PiMemoryBridgeError, match="memory_ingest_mode_invalid"):
        validate_memory_arguments(
            "document_upload",
            {
                "filename": "note.txt",
                "content_base64": "eA==",
                "ingest_mode": "unsafe",
            },
        )


def test_memory_connector_headers_require_safe_url_and_api_key():
    with pytest.raises(PiMemoryBridgeError, match="memory_connector_url_invalid"):
        memory_connector_headers(
            {
                "MEMORY_CONNECTOR_URL": "file:///tmp/memory",
                "CONNECTOR_API_KEY": "secret",
            }
        )
    with pytest.raises(PiMemoryBridgeError, match="memory_connector_api_key_missing"):
        memory_connector_headers({"MEMORY_CONNECTOR_URL": "https://memory.example/mcp"})
    with pytest.raises(PiMemoryBridgeError, match="memory_connector_url_insecure"):
        memory_connector_headers(
            {
                "MEMORY_CONNECTOR_URL": "http://memory.example/mcp",
                "CONNECTOR_API_KEY": "secret",
            }
        )
    url, _headers = memory_connector_headers(
        {
            "MEMORY_CONNECTOR_URL": "http://127.0.0.1:9999/mcp",
            "CONNECTOR_API_KEY": "secret",
        }
    )
    assert url == "http://127.0.0.1:9999/mcp"


def test_memory_document_upload_waits_for_terminal_processing_and_requires_one_record():
    arguments = {"filename": "note.txt", "content_base64": "eA=="}
    assert _memory_call_arguments("document_upload", arguments) == {
        **arguments,
        "wait_for_processing": True,
        "wait_timeout_seconds": 120,
    }

    confirmed, receipt = _write_confirmation(
        "document_upload",
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "ok": True,
                            "document_id": "document-1",
                            "processing_status": "completed",
                        }
                    ),
                }
            ]
        },
    )
    assert confirmed is True
    assert receipt == {
        "document_id": "document-1",
        "processing_status": "completed",
    }

    confirmed, receipt = _write_confirmation(
        "document_upload",
        {
            "content": [
                {"document_id": "unrelated-document"},
                {"processing_status": "completed"},
            ]
        },
    )
    assert confirmed is False
    assert receipt == {}

    confirmed, receipt = _write_confirmation(
        "document_upload",
        {
            "document_id": "document-pending",
            "processing_status": "processing",
        },
    )
    assert confirmed is False
    assert receipt == {}


def test_memory_bridge_calls_fake_mcp_with_authenticated_acl_headers(monkeypatch):
    with _memory_server() as (url, captured_headers):
        monkeypatch.setenv("MEMORY_CONNECTOR_URL", url)
        monkeypatch.setenv("CONNECTOR_API_KEY", "memory-secret")
        monkeypatch.setenv("MEMORY_CONNECTOR_AUTH_TYPE", "api_key")
        monkeypatch.setenv("MEMORY_CONNECTOR_CONTENT_TYPE", "application/json")

        response = asyncio.run(
            call_memory_tool("memory_recall", {"query": "project history"})
        )

    assert response["ok"] is True
    assert response["tool"] == "memory_recall"
    assert response["confirmed"] is False
    assert any(
        headers.get("authorization") == "Bearer memory-secret"
        and headers.get("x-friday-memory-auth-type") == "api_key"
        for headers in captured_headers
    )


def test_memory_bridge_confirms_memory_write_only_with_episode_receipt(monkeypatch):
    with _memory_server() as (url, _captured_headers):
        monkeypatch.setenv("MEMORY_CONNECTOR_URL", url)
        monkeypatch.setenv("CONNECTOR_API_KEY", "memory-secret")

        response = asyncio.run(
            call_memory_tool(
                "memory_write",
                {
                    "data": "Friday confirmed a durable preference.",
                    "type": "text",
                    "created_at": "2026-08-08T00:00:00Z",
                },
            )
        )

    assert response["confirmed"] is True
    assert response["receipt"] == {
        "episode_uuid": "episode-1",
        "processing_status": "completed",
    }
