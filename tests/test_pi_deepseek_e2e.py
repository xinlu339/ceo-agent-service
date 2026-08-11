from __future__ import annotations

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
def _fake_chat_completions_provider():
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
            chunks = [
                {
                    "id": "chatcmpl-deepseek-1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": '{"status":"ok"}',
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-deepseek-1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                },
            ]
            body = "".join(
                f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                for chunk in chunks
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


def test_legacy_deepseek_responses_config_runs_real_pi_over_chat_completions(
    tmp_path,
    monkeypatch,
):
    assert pi_cli_path().is_file(), "sibling Pi CLI build is required for this test"
    with _fake_chat_completions_provider() as (provider_url, requests):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-codex-home"))
        monkeypatch.setenv("CEO_PI_PROVIDER", "openai")
        monkeypatch.setenv("CEO_PI_MODEL", "deepseek-v4-pro")
        monkeypatch.setenv("CEO_PI_MODEL_SOURCE", "builtin")
        monkeypatch.setenv("CEO_PI_API", "openai-responses")
        monkeypatch.setenv("CEO_PI_BASE_URL", provider_url)
        monkeypatch.setenv("CEO_PI_API_KEY", "fake-deepseek-secret")
        monkeypatch.setenv("CEO_PI_THINKING_LEVEL", "off")
        monkeypatch.setenv("CEO_PI_AGENT_DIR", str(tmp_path / "pi-agent"))
        monkeypatch.setenv("CEO_PI_SESSION_DIR", str(tmp_path / "pi-sessions"))

        prompt = "Return one short JSON object."
        runner = PiRunner(workspace=tmp_path)
        command = runner.build_command(
            prompt,
            session_id=None,
            use_output_schema=False,
            approval_policy="never",
            developer_instructions="Return JSON and do not call tools.",
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
    summary = summarize_pi_stream(completed.stdout)
    assert summary.saw_agent_start is True
    assert summary.saw_agent_end is True
    assert summary.saw_agent_settled is True
    assert len(requests) == 1
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["payload"]["model"] == "deepseek-v4-pro"
    headers = {
        str(key).lower(): value
        for key, value in requests[0]["headers"].items()
    }
    assert headers["authorization"] == "Bearer fake-deepseek-secret"
