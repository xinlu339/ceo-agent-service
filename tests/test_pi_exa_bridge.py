from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from app.pi_exa_bridge import (
    PiExaBridgeError,
    call_exa_tool,
    exa_mcp_url,
    validate_exa_arguments,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _exa_server():
    port = _free_port()
    exa = FastMCP(
        "test-exa",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        log_level="ERROR",
    )

    @exa.tool()
    def web_search_exa(query: str, numResults: int = 10) -> dict[str, object]:
        return {"query": query, "results": [{"url": "https://example.com"}] * numResults}

    @exa.tool()
    def web_fetch_exa(urls: list[str], maxCharacters: int = 3000) -> dict[str, object]:
        return {"urls": urls, "maxCharacters": maxCharacters, "text": "safe"}

    server = uvicorn.Server(
        uvicorn.Config(
            exa.streamable_http_app(),
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
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_exa_search_contract_is_bounded_and_rejects_extra_fields():
    assert validate_exa_arguments(
        "web_search_exa",
        {"query": "current Pi Agent release", "numResults": 3},
    ) == {"query": "current Pi Agent release", "numResults": 3}
    with pytest.raises(PiExaBridgeError, match="exa_numResults_invalid"):
        validate_exa_arguments(
            "web_search_exa",
            {"query": "current Pi Agent release", "numResults": 100},
        )
    with pytest.raises(PiExaBridgeError, match="exa_arguments_extra_fields"):
        validate_exa_arguments(
            "web_search_exa",
            {"query": "current Pi Agent release", "unsafe": True},
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://service.internal/private",
        "https://user:password@example.com/private",
    ],
)
def test_exa_fetch_rejects_non_public_or_credentialed_urls(url: str):
    with pytest.raises(PiExaBridgeError):
        validate_exa_arguments("web_fetch_exa", {"urls": [url]})


def test_exa_mcp_endpoint_defaults_to_public_service_and_rejects_insecure_remote():
    assert exa_mcp_url({}) == "https://mcp.exa.ai/mcp"
    with pytest.raises(PiExaBridgeError, match="exa_mcp_url_insecure"):
        exa_mcp_url({"CEO_PI_EXA_MCP_URL": "http://exa.example/mcp"})


def test_exa_bridge_calls_exact_fake_mcp_tool(monkeypatch):
    with _exa_server() as url:
        monkeypatch.setenv("CEO_PI_EXA_MCP_URL", url)
        response = asyncio.run(
            call_exa_tool(
                "web_search_exa",
                {"query": "Pi Agent", "numResults": 2},
            )
        )

    assert response["ok"] is True
    assert response["tool"] == "web_search_exa"
    assert response["confirmed"] is False
    assert response["receipt"] == {}
    assert response["result"]["isError"] is False
