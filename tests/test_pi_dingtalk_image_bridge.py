from __future__ import annotations

from email.message import Message

import pytest

from app.pi_dingtalk_image_bridge import (
    PiDingtalkImageBridgeError,
    call_dingtalk_image_tool,
    validate_dingtalk_image_arguments,
)


class FakeDws:
    def __init__(self, payload: object):
        self.payload = payload
        self.calls: list[str] = []

    def download_robot_message_file(self, download_code: str):
        self.calls.append(download_code)
        return self.payload


class FakeResponse:
    def __init__(self, data: bytes, url: str = "https://cdn.example/image"):
        self.data = data
        self.url = url
        self.headers = Message()
        self.headers["Content-Length"] = str(len(data))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self.url

    def read(self, limit: int) -> bytes:
        return self.data[:limit]


def test_dingtalk_image_bridge_resolves_signed_url_without_returning_it():
    png = b"\x89PNG\r\n\x1a\nreviewed"
    dws = FakeDws(
        {"response": {"content": {"result": {"downloadUrl": "https://cdn.example/private?token=secret"}}}}
    )
    requested_urls: list[str] = []

    def opener(request, timeout):
        assert timeout == 30
        requested_urls.append(request.full_url)
        return FakeResponse(png)

    response = call_dingtalk_image_tool(
        {"download_code": "download-code-1"},
        dws=dws,
        opener=opener,
    )

    assert dws.calls == ["download-code-1"]
    assert requested_urls == ["https://cdn.example/private?token=secret"]
    assert response["effect"] == "read"
    assert response["confirmed"] is False
    assert response["result"]["mime_type"] == "image/png"
    assert "downloadUrl" not in str(response)
    assert "token=secret" not in str(response)


def test_dingtalk_image_bridge_rejects_invalid_arguments_and_private_urls():
    with pytest.raises(
        PiDingtalkImageBridgeError,
        match="dingtalk_image_arguments_invalid",
    ):
        validate_dingtalk_image_arguments({"download_code": "code", "extra": True})

    with pytest.raises(
        PiDingtalkImageBridgeError,
        match="dingtalk_image_url_insecure",
    ):
        call_dingtalk_image_tool(
            {"download_code": "code"},
            dws=FakeDws({"downloadUrl": "http://127.0.0.1/private"}),
            opener=lambda *_args, **_kwargs: FakeResponse(b""),
        )


def test_dingtalk_image_bridge_rejects_non_image_payload():
    with pytest.raises(
        PiDingtalkImageBridgeError,
        match="dingtalk_image_format_unsupported",
    ):
        call_dingtalk_image_tool(
            {"download_code": "code"},
            dws=FakeDws({"downloadUrl": "https://cdn.example/file"}),
            opener=lambda *_args, **_kwargs: FakeResponse(b"not-an-image"),
        )
