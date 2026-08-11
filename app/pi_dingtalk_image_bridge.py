from __future__ import annotations

import base64
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dws_client import DwsClient


TOOL_NAME = "download_dingtalk_image"
MAX_REQUEST_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOWNLOAD_CODE_CHARACTERS = 4096


class PiDingtalkImageBridgeError(RuntimeError):
    pass


def validate_dingtalk_image_arguments(arguments: object) -> dict[str, str]:
    if not isinstance(arguments, dict) or set(arguments) != {"download_code"}:
        raise PiDingtalkImageBridgeError("dingtalk_image_arguments_invalid")
    download_code = arguments.get("download_code")
    if (
        not isinstance(download_code, str)
        or not download_code.strip()
        or len(download_code) > MAX_DOWNLOAD_CODE_CHARACTERS
        or any(character in download_code for character in ("\x00", "\r", "\n"))
    ):
        raise PiDingtalkImageBridgeError("dingtalk_image_download_code_invalid")
    return {"download_code": download_code.strip()}


def call_dingtalk_image_tool(
    arguments: dict[str, str],
    *,
    dws: DwsClient | None = None,
    opener: Callable[..., BinaryIO] = urlopen,
) -> dict[str, object]:
    download_code = arguments["download_code"]
    client = dws or DwsClient(
        ding_robot_name=os.getenv("CEO_DING_ROBOT_NAME", "").strip() or None,
    )
    try:
        payload = client.download_robot_message_file(download_code)
    except Exception as exc:
        raise PiDingtalkImageBridgeError("dingtalk_image_download_url_failed") from exc
    download_url = _find_download_url(payload)
    _validate_download_url(download_url)
    request = Request(
        download_url,
        headers={"User-Agent": "ceo-agent-service-pi-image/1"},
        method="GET",
    )
    try:
        with opener(request, timeout=30) as response:
            final_url = str(getattr(response, "geturl", lambda: download_url)())
            _validate_download_url(final_url)
            content_length = str(response.headers.get("Content-Length") or "").strip()
            if content_length.isdigit() and int(content_length) > MAX_IMAGE_BYTES:
                raise PiDingtalkImageBridgeError("dingtalk_image_too_large")
            image = response.read(MAX_IMAGE_BYTES + 1)
    except PiDingtalkImageBridgeError:
        raise
    except Exception as exc:
        raise PiDingtalkImageBridgeError("dingtalk_image_fetch_failed") from exc
    if not image:
        raise PiDingtalkImageBridgeError("dingtalk_image_empty")
    if len(image) > MAX_IMAGE_BYTES:
        raise PiDingtalkImageBridgeError("dingtalk_image_too_large")
    mime_type = _image_mime_type(image)
    if not mime_type:
        raise PiDingtalkImageBridgeError("dingtalk_image_format_unsupported")
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "result": {
            "data": base64.b64encode(image).decode("ascii"),
            "mime_type": mime_type,
            "byte_length": len(image),
        },
        "effect": "read",
        "confirmed": False,
        "receipt": {},
    }


def _find_download_url(value: object) -> str:
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if key.casefold() == "downloadurl" and isinstance(item, str):
                    if item.strip():
                        return item.strip()
                if isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
    raise PiDingtalkImageBridgeError("dingtalk_image_download_url_missing")


def _validate_download_url(value: str) -> None:
    if not value or len(value) > 8192 or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise PiDingtalkImageBridgeError("dingtalk_image_url_invalid")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PiDingtalkImageBridgeError("dingtalk_image_url_insecure")
    if parsed.username or parsed.password:
        raise PiDingtalkImageBridgeError("dingtalk_image_url_credentials_forbidden")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal", ".lan", ".home")
    ):
        raise PiDingtalkImageBridgeError("dingtalk_image_url_private_host_forbidden")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise PiDingtalkImageBridgeError("dingtalk_image_url_private_host_forbidden")


def _image_mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise PiDingtalkImageBridgeError("dingtalk_image_request_too_large")
    try:
        request: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiDingtalkImageBridgeError("dingtalk_image_request_invalid") from exc
    if not isinstance(request, dict) or request.get("tool") != TOOL_NAME:
        raise PiDingtalkImageBridgeError("dingtalk_image_tool_not_allowed")
    arguments = validate_dingtalk_image_arguments(request.get("arguments"))
    response = call_dingtalk_image_tool(arguments)
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PiDingtalkImageBridgeError as exc:
        sys.stderr.write(str(exc))
        raise SystemExit(2)
