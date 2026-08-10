from __future__ import annotations

import asyncio
import base64
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

from app.store import AutoReplyStore


MEMORY_CONNECTOR_URL_ENV = "MEMORY_CONNECTOR_URL"
MEMORY_CONNECTOR_API_KEY_ENV = "CONNECTOR_API_KEY"
MEMORY_CONNECTOR_AUTH_TYPE_ENV = "MEMORY_CONNECTOR_AUTH_TYPE"
MEMORY_CONNECTOR_CONTENT_TYPE_ENV = "MEMORY_CONNECTOR_CONTENT_TYPE"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
READ_TOOLS = frozenset({"user_get", "memory_recall", "memory_get", "timeline_get"})
WRITE_TOOLS = frozenset({"memory_write", "document_upload"})
_DOCUMENT_RECEIPT_IDENTIFIER_KEYS = (
    "document_id",
    "upload_id",
)
_DOCUMENT_COMPLETED_STATES = frozenset(
    {"completed", "success", "succeeded", "done"}
)


class PiMemoryBridgeError(RuntimeError):
    pass


def validate_memory_arguments(tool: str, arguments: object) -> dict[str, object]:
    if tool not in READ_TOOLS | WRITE_TOOLS:
        raise PiMemoryBridgeError("memory_tool_not_allowed")
    if not isinstance(arguments, dict):
        raise PiMemoryBridgeError("memory_arguments_invalid")
    forbidden = {"user_id", "graph_id", "graph_ids"} & set(arguments)
    if forbidden:
        raise PiMemoryBridgeError("memory_acl_argument_forbidden")
    allowed: dict[str, frozenset[str]] = {
        "user_get": frozenset({"query"}),
        "memory_recall": frozenset({"query"}),
        "memory_get": frozenset({"uuid"}),
        "timeline_get": frozenset({"thread_id"}),
        "memory_write": frozenset({"data", "type", "created_at"}),
        "document_upload": frozenset(
            {
                "filename",
                "content_base64",
                "mime_type",
                "uploaded_at",
                "ingest_mode",
            }
        ),
    }
    if not set(arguments) <= allowed[tool]:
        raise PiMemoryBridgeError("memory_arguments_extra_fields")
    if tool == "user_get":
        _optional_text(arguments, "query", maximum=4096)
    elif tool == "memory_recall":
        _required_text(arguments, "query", maximum=8192)
    elif tool == "memory_get":
        _required_text(arguments, "uuid", maximum=256)
    elif tool == "timeline_get":
        _required_text(arguments, "thread_id", maximum=256)
    elif tool == "memory_write":
        _required_text(arguments, "data", maximum=512 * 1024)
        _required_text(arguments, "type", maximum=64)
        _required_text(arguments, "created_at", maximum=128)
    elif tool == "document_upload":
        filename = _required_text(arguments, "filename", maximum=512)
        if Path(filename).name != filename or "\x00" in filename:
            raise PiMemoryBridgeError("memory_filename_invalid")
        encoded = _required_text(
            arguments,
            "content_base64",
            maximum=(MAX_DOCUMENT_BYTES * 4 // 3) + 16,
        )
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise PiMemoryBridgeError("memory_document_base64_invalid") from exc
        if len(decoded) > MAX_DOCUMENT_BYTES:
            raise PiMemoryBridgeError("memory_document_too_large")
        _optional_text(arguments, "mime_type", maximum=256)
        _optional_text(arguments, "uploaded_at", maximum=128)
        ingest_mode = arguments.get("ingest_mode")
        if ingest_mode is not None and ingest_mode not in {"semantic", "graph"}:
            raise PiMemoryBridgeError("memory_ingest_mode_invalid")
    return dict(arguments)


def memory_connector_headers(env: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    values = os.environ if env is None else env
    url = values.get(MEMORY_CONNECTOR_URL_ENV, "").strip().rstrip("/")
    token = values.get(MEMORY_CONNECTOR_API_KEY_ENV, "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PiMemoryBridgeError("memory_connector_url_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PiMemoryBridgeError("memory_connector_url_invalid")
    if parsed.scheme == "http" and not _loopback_hostname(parsed.hostname or ""):
        raise PiMemoryBridgeError("memory_connector_url_insecure")
    if not token:
        raise PiMemoryBridgeError("memory_connector_api_key_missing")
    return url, {
        "Authorization": f"Bearer {token}",
        "X-Friday-Memory-Auth-Type": values.get(
            MEMORY_CONNECTOR_AUTH_TYPE_ENV,
            "api_key",
        ).strip()
        or "api_key",
        "Content-Type": values.get(
            MEMORY_CONNECTOR_CONTENT_TYPE_ENV,
            "application/json",
        ).strip()
        or "application/json",
    }


async def call_memory_tool(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    url, headers = memory_connector_headers()
    call_arguments = _memory_call_arguments(tool, arguments)
    async with streamablehttp_client(
        url,
        headers=headers,
        timeout=30,
        sse_read_timeout=120,
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                tool,
                call_arguments,
                read_timeout_seconds=timedelta(seconds=120),
            )
    payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    if result.isError is True:
        raise PiMemoryBridgeError("memory_tool_failed")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_RESULT_BYTES:
        raise PiMemoryBridgeError("memory_result_too_large")
    confirmed, receipt = _write_confirmation(tool, payload)
    return {
        "ok": True,
        "tool": tool,
        "result": payload,
        "confirmed": confirmed,
        "receipt": receipt,
    }


def _write_confirmation(
    tool: str,
    payload: dict[str, object],
) -> tuple[bool, dict[str, str]]:
    if tool == "memory_write":
        parsed = AutoReplyStore._parse_memory_write_output(
            json.dumps(payload, ensure_ascii=False)
        )
        episode_uuid = parsed.get("memory_episode_id", "")
        if parsed.get("status") == "written" and episode_uuid:
            return True, {
                "episode_uuid": episode_uuid,
                "processing_status": "completed",
            }
        return False, {}
    if tool == "document_upload":
        receipt = _confirmed_document_upload_receipt(payload)
        if receipt:
            return True, receipt
    return False, {}


def _memory_call_arguments(
    tool: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    if tool != "document_upload":
        return arguments
    return {
        **arguments,
        "wait_for_processing": True,
        "wait_timeout_seconds": 120,
    }


def _confirmed_document_upload_receipt(value: object) -> dict[str, str]:
    for record in _mapping_records(value):
        if record.get("ok") is False:
            continue
        identifier = next(
            (
                item.strip()[:500]
                for key in _DOCUMENT_RECEIPT_IDENTIFIER_KEYS
                for item in [record.get(key)]
                if isinstance(item, str) and item.strip()
            ),
            "",
        )
        state = next(
            (
                item.strip().casefold()
                for key in ("processing_status", "status", "state")
                for item in [record.get(key)]
                if isinstance(item, str)
                and item.strip().casefold() in _DOCUMENT_COMPLETED_STATES
            ),
            "",
        )
        if identifier and state:
            return {
                "document_id": identifier,
                "processing_status": "completed",
            }
    return {}


def _mapping_records(value: object):
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            candidate = current.strip()
            if candidate.startswith(("{", "[")):
                try:
                    stack.append(json.loads(candidate))
                except json.JSONDecodeError:
                    pass
            continue
        if isinstance(current, list):
            stack.extend(current[:256])
            continue
        if not isinstance(current, dict):
            continue
        yield current
        stack.extend(current.values())


def _loopback_hostname(hostname: str) -> bool:
    normalized = hostname.casefold().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _required_text(arguments: dict[str, object], key: str, *, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PiMemoryBridgeError(f"memory_{key}_invalid")
    return value


def _optional_text(arguments: dict[str, object], key: str, *, maximum: int) -> str:
    value = arguments.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise PiMemoryBridgeError(f"memory_{key}_invalid")
    return value


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise PiMemoryBridgeError("memory_request_too_large")
    try:
        request = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiMemoryBridgeError("memory_request_invalid") from exc
    if not isinstance(request, dict):
        raise PiMemoryBridgeError("memory_request_invalid")
    tool = request.get("tool")
    if not isinstance(tool, str):
        raise PiMemoryBridgeError("memory_tool_not_allowed")
    arguments = validate_memory_arguments(tool, request.get("arguments"))
    response = asyncio.run(call_memory_tool(tool, arguments))
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PiMemoryBridgeError as exc:
        sys.stderr.write(str(exc))
        raise SystemExit(2)
