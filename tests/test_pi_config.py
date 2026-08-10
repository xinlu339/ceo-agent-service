import json
import os
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app.audit_web import create_audit_app, handle_agent_config_post, render_config_page
from app.config import read_env_file
from app.pi_runner import (
    PI_API_KEY_ENV,
    PI_EXA_MCP_URL_ENV,
    PI_XIAOQING_ACCESS_TOKEN_ENV,
    PI_XIAOQING_MCP_URL_ENV,
    pi_models_config_for_values,
    pi_cli_path,
    pi_node_binary,
    validate_pi_base_url,
)


def _agent_form(tmp_path: Path, **overrides: str) -> bytes:
    values = {
        "pi_node_binary": pi_node_binary(),
        "pi_cli_path": str(pi_cli_path()),
        "pi_provider": "openai",
        "pi_model": "gpt-5.5",
        "pi_api": "openai-responses",
        "pi_base_url": "https://gateway.example/v1",
        "pi_exa_mcp_url": "https://mcp.exa.ai/mcp",
        "pi_xiaoqing_mcp_url": "https://interview.hr.startask.net/mcp",
        "pi_xiaoqing_access_token": "",
        "pi_api_key": "",
        "pi_thinking": "medium",
        "pi_agent_dir": str(tmp_path / "pi-agent"),
        "pi_session_dir": str(tmp_path / "pi-sessions"),
    }
    values.update(overrides)
    return urlencode(values).encode()


def test_pi_agent_config_page_never_renders_existing_api_key(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"{PI_API_KEY_ENV}=super-secret-key\n"
        f"{PI_XIAOQING_ACCESS_TOKEN_ENV}=xiaoqing-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))
    monkeypatch.setenv(PI_API_KEY_ENV, "super-secret-key")
    monkeypatch.setenv(PI_XIAOQING_ACCESS_TOKEN_ENV, "xiaoqing-secret")

    html = render_config_page(active_tab="agent")

    assert "Pi Agent runtime" in html
    assert "Configured" in html
    assert "super-secret-key" not in html
    assert "xiaoqing-secret" not in html
    assert 'type="password"' in html
    assert 'value="" autocomplete="new-password"' in html
    assert 'action="/config/agent"' in html
    assert "Memory reviewed bridge" in html
    assert "DWS reviewed tools" in html
    assert "Friday Memory tools" in html
    assert "Exa MCP URL" in html
    assert "Xiaoqing MCP URL" in html
    assert "Xiaoqing OAuth token" in html
    assert "Xiaoqing Interview" in html
    assert (
        "Pi runtime is ready; integrations need setup" in html
        or "Pi runtime needs configuration" in html
    )
    assert "仍需配置或认证" in html
    assert "API protocol 必须与 Pi 内置模型的真实协议一致" in html
    assert "状态会区分缺少本地配置、缺少 OAuth/CLI 登录和工具不可用" in html
    assert "Unsupported" not in html
    assert "Pi bash" not in html


def test_pi_agent_config_preserves_blank_api_key_and_writes_reference_only(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"{PI_API_KEY_ENV}=existing-secret\n"
        f"{PI_XIAOQING_ACCESS_TOKEN_ENV}=existing-xiaoqing-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))
    monkeypatch.setenv(PI_API_KEY_ENV, "existing-secret")
    monkeypatch.setenv(
        PI_XIAOQING_ACCESS_TOKEN_ENV,
        "existing-xiaoqing-secret",
    )

    status, headers, html = handle_agent_config_post(_agent_form(tmp_path))

    assert status == 303
    assert headers["Location"] == "/config?tab=agent&saved=1"
    assert html == ""
    assert f"{PI_API_KEY_ENV}=existing-secret" in env_path.read_text(encoding="utf-8")
    assert (
        f"{PI_XIAOQING_ACCESS_TOKEN_ENV}=existing-xiaoqing-secret"
        in env_path.read_text(encoding="utf-8")
    )
    assert env_path.stat().st_mode & 0o777 == 0o600
    models_text = (tmp_path / "pi-agent" / "models.json").read_text(
        encoding="utf-8"
    )
    assert "existing-secret" not in models_text
    assert json.loads(models_text)["providers"]["openai"]["apiKey"] == (
        f"${PI_API_KEY_ENV}"
    )
    assert (
        f"{PI_EXA_MCP_URL_ENV}=https://mcp.exa.ai/mcp"
        in env_path.read_text(encoding="utf-8")
    )
    assert (
        f"{PI_XIAOQING_MCP_URL_ENV}=https://interview.hr.startask.net/mcp"
        in env_path.read_text(encoding="utf-8")
    )


def test_pi_agent_config_can_replace_and_explicitly_clear_api_key(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))

    status, _, _ = handle_agent_config_post(
        _agent_form(tmp_path, pi_api_key="new-secret")
    )

    assert status == 303
    assert os.environ[PI_API_KEY_ENV] == "new-secret"
    assert f"{PI_API_KEY_ENV}=new-secret" in env_path.read_text(encoding="utf-8")

    status, _, _ = handle_agent_config_post(
        _agent_form(
            tmp_path,
            pi_api_key="",
            clear_pi_api_key="1",
        )
    )

    assert status == 303
    assert os.environ[PI_API_KEY_ENV] == ""
    assert f'{PI_API_KEY_ENV}=""\n' in env_path.read_text(encoding="utf-8")


def test_pi_agent_config_persists_secret_values_literally(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))
    secret = 'sk-$NOT_AN_ENV-"quoted"-\\path with space'

    status, _, _ = handle_agent_config_post(
        _agent_form(tmp_path, pi_api_key=secret)
    )

    assert status == 303
    assert read_env_file(env_path)[PI_API_KEY_ENV] == secret
    assert os.environ[PI_API_KEY_ENV] == secret


def test_pi_agent_config_can_replace_and_clear_xiaoqing_token(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))

    status, _, _ = handle_agent_config_post(
        _agent_form(
            tmp_path,
            pi_xiaoqing_access_token="new-xiaoqing-secret",
        )
    )

    assert status == 303
    assert os.environ[PI_XIAOQING_ACCESS_TOKEN_ENV] == "new-xiaoqing-secret"
    assert (
        f"{PI_XIAOQING_ACCESS_TOKEN_ENV}=new-xiaoqing-secret"
        in env_path.read_text(encoding="utf-8")
    )

    status, _, _ = handle_agent_config_post(
        _agent_form(
            tmp_path,
            clear_pi_xiaoqing_access_token="1",
        )
    )

    assert status == 303
    assert os.environ[PI_XIAOQING_ACCESS_TOKEN_ENV] == ""
    assert (
        f'{PI_XIAOQING_ACCESS_TOKEN_ENV}=""\n'
        in env_path.read_text(encoding="utf-8")
    )


def test_pi_agent_config_rejects_invalid_base_url_without_saving_secret(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))

    status, _, html = handle_agent_config_post(
        _agent_form(
            tmp_path,
            pi_base_url="file:///tmp/provider",
            pi_api_key="must-not-be-written",
        )
    )

    assert status == 400
    assert "absolute http(s) URL" in html
    assert "must-not-be-written" not in html
    assert not env_path.exists()


def test_pi_provider_base_url_requires_https_except_for_loopback():
    assert validate_pi_base_url("http://127.0.0.1:8080/v1") == (
        "http://127.0.0.1:8080/v1"
    )
    assert validate_pi_base_url("http://localhost:8080/v1") == (
        "http://localhost:8080/v1"
    )
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_pi_base_url("http://provider.example/v1")


def test_custom_model_config_uses_pi_conservative_defaults():
    config = pi_models_config_for_values(
        provider="custom-provider",
        model="custom-model",
        api="openai-responses",
        base_url="https://gateway.example/v1",
    )

    model = config["providers"]["custom-provider"]["models"][0]
    assert model == {"id": "custom-model", "name": "custom-model"}


def test_pi_agent_config_rejects_api_protocol_that_would_be_silently_ignored(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))

    status, _, html = handle_agent_config_post(
        _agent_form(
            tmp_path,
            pi_api="openai-completions",
            pi_base_url="",
            pi_api_key="must-not-be-written",
        )
    )

    assert status == 400
    assert "Requested API openai-completions resolved to" in html
    assert "must-not-be-written" not in html
    assert not env_path.exists()


def test_pi_agent_config_rejects_insecure_external_exa_url(tmp_path: Path, monkeypatch):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))

    status, _, html = handle_agent_config_post(
        _agent_form(
            tmp_path,
            pi_exa_mcp_url="http://exa.example/mcp",
            pi_api_key="must-not-be-written",
        )
    )

    assert status == 400
    assert "exa_mcp_url_insecure" in html
    assert "must-not-be-written" not in html
    assert not env_path.exists()


def test_pi_agent_config_routes_are_loopback_only_and_never_echo_secret(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))
    app = create_audit_app(tmp_path / "worker.sqlite3")
    loopback = TestClient(
        app,
        client=("127.0.0.1", 50000),
        headers={"Host": "127.0.0.1:8765"},
    )

    page = loopback.get("/config?tab=agent")
    saved = loopback.post(
        "/config/agent",
        content=_agent_form(tmp_path, pi_api_key="route-secret"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    external = TestClient(
        app,
        client=("203.0.113.10", 50000),
        headers={"Host": "example.test"},
    ).post(
        "/config/agent",
        content=_agent_form(tmp_path, pi_api_key="must-not-save"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert page.status_code == 200
    assert "Pi Agent runtime" in page.text
    assert "route-secret" not in page.text
    assert saved.status_code == 303
    assert saved.headers["location"] == "/config?tab=agent&saved=1"
    assert "route-secret" not in saved.text
    assert external.status_code == 403
    assert "must-not-save" not in external.text


def test_pi_agent_config_route_rejects_non_form_content_type(
    tmp_path: Path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("CEO_ENV_FILE", str(env_path))
    client = TestClient(
        create_audit_app(tmp_path / "worker.sqlite3"),
        client=("127.0.0.1", 50000),
        headers={"Host": "127.0.0.1:8765"},
    )

    response = client.post(
        "/config/agent",
        content=_agent_form(tmp_path, pi_api_key="must-not-save"),
        headers={
            "Content-Type": "text/plain",
            "Origin": "http://127.0.0.1:8765",
        },
    )

    assert response.status_code == 415
    assert not env_path.exists()


def test_legacy_codex_routes_redirect_to_pi(tmp_path: Path):
    client = TestClient(create_audit_app(tmp_path / "worker.sqlite3"))

    session_list = client.get("/codex", follow_redirects=False)
    session_detail = client.get("/codex/session-1", follow_redirects=False)

    assert session_list.status_code == 303
    assert session_list.headers["location"] == "/pi"
    assert session_detail.status_code == 303
    assert session_detail.headers["location"] == "/pi/session-1"
