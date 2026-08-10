from __future__ import annotations

import asyncio
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


XIAOQING_MCP_URL_ENV = "CEO_PI_XIAOQING_MCP_URL"
XIAOQING_ACCESS_TOKEN_ENV = "CEO_PI_XIAOQING_ACCESS_TOKEN"
DEFAULT_XIAOQING_MCP_URL = "https://interview.hr.startask.net/mcp"
READ_TOOLS = frozenset(
    {
        "search_candidates",
        "get_dashboard_stats",
        "get_interview_context",
        "download_attachment",
        "list_candidate_interviews",
    }
)
WRITE_TOOLS = frozenset({"upload_interview_result"})
ALLOWED_TOOLS = READ_TOOLS | WRITE_TOOLS
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_ARGUMENT_DEPTH = 32
MAX_ARGUMENT_ITEMS = 10_000
_COMPLETED_STATES = frozenset(
    {"completed", "success", "succeeded", "done", "uploaded", "created"}
)
_RECEIPT_IDENTIFIER_KEYS = (
    "interview_result_id",
    "result_id",
    "record_id",
    "upload_id",
    "feedback_id",
)
_REQUEST_IDENTITY_KEYS = (
    "candidate_id",
    "interview_id",
    "interview_record_id",
)


class PiXiaoqingBridgeError(RuntimeError):
    pass


def xiaoqing_mcp_url(env: dict[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    value = (
        values.get(XIAOQING_MCP_URL_ENV, DEFAULT_XIAOQING_MCP_URL)
        .strip()
        .rstrip("/")
    )
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PiXiaoqingBridgeError("xiaoqing_mcp_url_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PiXiaoqingBridgeError("xiaoqing_mcp_url_invalid")
    if parsed.scheme == "http" and not _loopback_hostname(parsed.hostname or ""):
        raise PiXiaoqingBridgeError("xiaoqing_mcp_url_insecure")
    return value


def xiaoqing_headers(env: dict[str, str] | None = None) -> dict[str, str]:
    values = os.environ if env is None else env
    token = values.get(XIAOQING_ACCESS_TOKEN_ENV, "").strip()
    if not token:
        return {}
    if (
        len(token) > 32_768
        or any(character in token for character in ("\x00", "\r", "\n"))
        or token.casefold().startswith("bearer ")
    ):
        raise PiXiaoqingBridgeError("xiaoqing_access_token_invalid")
    return {"Authorization": f"Bearer {token}"}


def validate_xiaoqing_arguments(
    tool: str,
    arguments: object,
) -> dict[str, object]:
    if tool not in ALLOWED_TOOLS:
        raise PiXiaoqingBridgeError("xiaoqing_tool_not_allowed")
    if not isinstance(arguments, dict):
        raise PiXiaoqingBridgeError("xiaoqing_arguments_invalid")
    _validate_json_value(arguments)
    serialized = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise PiXiaoqingBridgeError("xiaoqing_arguments_too_large")
    dry_run = arguments.get("dry_run")
    if tool != "upload_interview_result" and "dry_run" in arguments:
        raise PiXiaoqingBridgeError("xiaoqing_dry_run_not_supported")
    if tool == "upload_interview_result" and dry_run is not None:
        if not isinstance(dry_run, bool):
            raise PiXiaoqingBridgeError("xiaoqing_dry_run_invalid")
    return dict(arguments)


async def call_xiaoqing_tool(
    tool: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    try:
        async with streamablehttp_client(
            xiaoqing_mcp_url(),
            headers=xiaoqing_headers(),
            timeout=30,
            sse_read_timeout=120,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                descriptor = next(
                    (candidate for candidate in listed.tools if candidate.name == tool),
                    None,
                )
                if descriptor is None:
                    raise PiXiaoqingBridgeError("xiaoqing_tool_unavailable")
                _validate_listed_schema(arguments, descriptor.inputSchema)
                result = await session.call_tool(
                    tool,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=120),
                )
    except BaseExceptionGroup as exc:
        reviewed_error = _find_reviewed_error(exc)
        if reviewed_error is not None:
            raise reviewed_error from exc
        raise PiXiaoqingBridgeError("xiaoqing_mcp_unavailable") from exc
    payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    if result.isError is True:
        raise PiXiaoqingBridgeError("xiaoqing_tool_failed")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_RESULT_BYTES:
        raise PiXiaoqingBridgeError("xiaoqing_result_too_large")
    dry_run = tool == "upload_interview_result" and arguments.get("dry_run") is True
    receipt = (
        {}
        if tool in READ_TOOLS or dry_run
        else _confirmed_upload_receipt(payload, arguments)
    )
    return {
        "ok": True,
        "tool": tool,
        "result": payload,
        "effect": "read" if tool in READ_TOOLS or dry_run else "write",
        "confirmed": bool(receipt),
        "receipt": receipt,
    }


def _validate_listed_schema(
    arguments: dict[str, object],
    schema: object,
) -> None:
    if not isinstance(schema, dict):
        raise PiXiaoqingBridgeError("xiaoqing_tool_schema_invalid")
    properties = schema.get("properties")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(item, str) for item in required
    ):
        raise PiXiaoqingBridgeError("xiaoqing_tool_schema_invalid")
    missing = [key for key in required if key not in arguments]
    if missing:
        raise PiXiaoqingBridgeError("xiaoqing_required_argument_missing")
    if isinstance(properties, dict) and schema.get("additionalProperties") is False:
        if not set(arguments) <= set(properties):
            raise PiXiaoqingBridgeError("xiaoqing_arguments_extra_fields")


def _find_reviewed_error(group: BaseExceptionGroup) -> PiXiaoqingBridgeError | None:
    for error in group.exceptions:
        if isinstance(error, PiXiaoqingBridgeError):
            return error
        if isinstance(error, BaseExceptionGroup):
            nested = _find_reviewed_error(error)
            if nested is not None:
                return nested
    return None


def _confirmed_upload_receipt(
    payload: object,
    arguments: dict[str, object] | None = None,
) -> dict[str, str]:
    expected_identities = {
        key: value.strip()
        for key in _REQUEST_IDENTITY_KEYS
        for value in [(arguments or {}).get(key)]
        if isinstance(value, str) and value.strip()
    }
    for record in _mapping_records(payload):
        identifier = next(
            (
                value.strip()[:500]
                for key in _RECEIPT_IDENTIFIER_KEYS
                for value in [record.get(key)]
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        completed_state = next(
            (
                value.strip().casefold()
                for key in ("processing_status", "status", "state")
                for value in [record.get(key)]
                if isinstance(value, str)
                and value.strip().casefold() in _COMPLETED_STATES
            ),
            "",
        )
        identity_matches = not expected_identities or any(
            isinstance(record.get(key), str)
            and str(record[key]).strip() == expected
            for key, expected in expected_identities.items()
        )
        if identifier and completed_state and identity_matches:
            return {
                "result_record_id": identifier,
                "processing_status": "completed",
            }
    return {}


def _mapping_records(value: object):
    stack: list[object] = [value]
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


def _validate_json_value(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    item_count = 0
    while stack:
        current, depth = stack.pop()
        item_count += 1
        if item_count > MAX_ARGUMENT_ITEMS or depth > MAX_ARGUMENT_DEPTH:
            raise PiXiaoqingBridgeError("xiaoqing_arguments_too_complex")
        if current is None or isinstance(current, (str, int, float, bool)):
            if isinstance(current, str) and any(
                character in current for character in ("\x00", "\r")
            ):
                raise PiXiaoqingBridgeError("xiaoqing_arguments_invalid")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > 256
                    or any(character in key for character in ("\x00", "\r", "\n"))
                ):
                    raise PiXiaoqingBridgeError("xiaoqing_arguments_invalid")
                stack.append((item, depth + 1))
            continue
        raise PiXiaoqingBridgeError("xiaoqing_arguments_invalid")


def _loopback_hostname(hostname: str) -> bool:
    normalized = hostname.casefold().rstrip(".")
    return normalized == "localhost" or normalized in {"127.0.0.1", "::1"}


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise PiXiaoqingBridgeError("xiaoqing_request_too_large")
    try:
        request: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiXiaoqingBridgeError("xiaoqing_request_invalid") from exc
    if not isinstance(request, dict):
        raise PiXiaoqingBridgeError("xiaoqing_request_invalid")
    tool = request.get("tool")
    if not isinstance(tool, str):
        raise PiXiaoqingBridgeError("xiaoqing_tool_not_allowed")
    arguments = validate_xiaoqing_arguments(tool, request.get("arguments"))
    response = asyncio.run(call_xiaoqing_tool(tool, arguments))
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PiXiaoqingBridgeError as exc:
        sys.stderr.write(str(exc))
        raise SystemExit(2)
