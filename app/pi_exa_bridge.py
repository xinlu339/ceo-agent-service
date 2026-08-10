from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


EXA_MCP_URL_ENV = "CEO_PI_EXA_MCP_URL"
DEFAULT_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
READ_TOOLS = frozenset({"web_search_exa", "web_fetch_exa"})
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_QUERY_CHARACTERS = 8192
MAX_FETCH_URLS = 10
MAX_FETCH_CHARACTERS = 20_000


class PiExaBridgeError(RuntimeError):
    pass


def validate_exa_arguments(tool: str, arguments: object) -> dict[str, object]:
    if tool not in READ_TOOLS:
        raise PiExaBridgeError("exa_tool_not_allowed")
    if not isinstance(arguments, dict):
        raise PiExaBridgeError("exa_arguments_invalid")
    allowed = {
        "web_search_exa": frozenset({"query", "numResults"}),
        "web_fetch_exa": frozenset({"urls", "maxCharacters"}),
    }
    if not set(arguments) <= allowed[tool]:
        raise PiExaBridgeError("exa_arguments_extra_fields")

    if tool == "web_search_exa":
        query = arguments.get("query")
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_QUERY_CHARACTERS
            or any(character in query for character in ("\x00", "\r"))
        ):
            raise PiExaBridgeError("exa_query_invalid")
        _optional_integer(arguments, "numResults", minimum=1, maximum=10)
    else:
        urls = arguments.get("urls")
        if (
            not isinstance(urls, list)
            or not urls
            or len(urls) > MAX_FETCH_URLS
            or not all(isinstance(url, str) for url in urls)
        ):
            raise PiExaBridgeError("exa_urls_invalid")
        for url in urls:
            _validate_public_web_url(url)
        _optional_integer(
            arguments,
            "maxCharacters",
            minimum=1,
            maximum=MAX_FETCH_CHARACTERS,
        )
    return dict(arguments)


def exa_mcp_url(env: dict[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    value = values.get(EXA_MCP_URL_ENV, DEFAULT_EXA_MCP_URL).strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PiExaBridgeError("exa_mcp_url_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PiExaBridgeError("exa_mcp_url_invalid")
    if parsed.scheme == "http" and not _loopback_hostname(parsed.hostname or ""):
        raise PiExaBridgeError("exa_mcp_url_insecure")
    return value


async def call_exa_tool(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    async with streamablehttp_client(
        exa_mcp_url(),
        timeout=30,
        sse_read_timeout=90,
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                tool,
                arguments,
                read_timeout_seconds=timedelta(seconds=90),
            )
    payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    if result.isError is True:
        raise PiExaBridgeError("exa_tool_failed")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_RESULT_BYTES:
        raise PiExaBridgeError("exa_result_too_large")
    return {
        "ok": True,
        "tool": tool,
        "result": payload,
        "confirmed": False,
        "receipt": {},
    }


def _optional_integer(
    arguments: dict[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PiExaBridgeError(f"exa_{key}_invalid")
    if value < minimum or value > maximum:
        raise PiExaBridgeError(f"exa_{key}_invalid")
    return value


def _validate_public_web_url(value: str) -> None:
    if not value or len(value) > 4096 or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise PiExaBridgeError("exa_url_invalid")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PiExaBridgeError("exa_url_invalid")
    if parsed.username or parsed.password:
        raise PiExaBridgeError("exa_url_credentials_forbidden")
    hostname = parsed.hostname.casefold().rstrip(".")
    if _non_public_hostname(hostname):
        raise PiExaBridgeError("exa_url_private_host_forbidden")


def _non_public_hostname(hostname: str) -> bool:
    if not hostname or hostname == "localhost":
        return True
    if hostname.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return not address.is_global


def _loopback_hostname(hostname: str) -> bool:
    if hostname.casefold().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise PiExaBridgeError("exa_request_too_large")
    try:
        request: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiExaBridgeError("exa_request_invalid") from exc
    if not isinstance(request, dict):
        raise PiExaBridgeError("exa_request_invalid")
    tool = request.get("tool")
    if not isinstance(tool, str):
        raise PiExaBridgeError("exa_tool_not_allowed")
    arguments = validate_exa_arguments(tool, request.get("arguments"))
    response = asyncio.run(call_exa_tool(tool, arguments))
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PiExaBridgeError as exc:
        sys.stderr.write(str(exc))
        raise SystemExit(2)
