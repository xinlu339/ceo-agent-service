import base64
import json
from pathlib import Path

import pytest

from app.memory_connector_config import (
    MEMORY_CONNECTOR_CONFIG_DIR_ENV,
    jwt_token_is_expired,
    memory_connector_config_dir,
    memory_connector_config_issue,
    memory_connector_env,
    memory_connector_env_from_config,
    memory_connector_env_from_plugin_config,
    parse_export_env_file,
)


@pytest.fixture(autouse=True)
def isolate_memory_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "memory-config"
    root.mkdir()
    monkeypatch.setenv(MEMORY_CONNECTOR_CONFIG_DIR_ENV, str(root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "legacy-codex-home"))
    for key in (
        "MEMORY_CONNECTOR_URL",
        "CONNECTOR_API_KEY",
        "MEMORY_CONNECTOR_AUTH_TYPE",
        "MEMORY_CONNECTOR_CONTENT_TYPE",
        "MEMORY_CONNECTOR_USER_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    return root


def unsigned_jwt(payload: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


def test_config_dir_prefers_neutral_override_and_keeps_legacy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    neutral = tmp_path / "neutral"
    legacy = tmp_path / "legacy"
    monkeypatch.setenv(MEMORY_CONNECTOR_CONFIG_DIR_ENV, str(neutral))
    monkeypatch.setenv("CODEX_HOME", str(legacy))
    assert memory_connector_config_dir() == neutral

    monkeypatch.delenv(MEMORY_CONNECTOR_CONFIG_DIR_ENV)
    assert memory_connector_config_dir() == legacy


def test_parse_export_env_file_supports_shell_quotes_and_comments(tmp_path: Path):
    path = tmp_path / "memory_connector.env"
    path.write_text(
        "export CONNECTOR_API_KEY='key with $ and spaces' # comment\n"
        "MEMORY_CONNECTOR_URL=https://memory.example/mcp\n",
        encoding="utf-8",
    )

    assert parse_export_env_file(path) == {
        "CONNECTOR_API_KEY": "key with $ and spaces",
        "MEMORY_CONNECTOR_URL": "https://memory.example/mcp",
    }


def test_memory_env_uses_reviewed_precedence_and_never_forwards_user_id(
    isolate_memory_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = isolate_memory_environment
    plugin_dir = root / "plugins" / "memory-connector"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory_connector": {
                        "url": "https://plugin.example/mcp",
                        "http_headers": {
                            "Authorization": "Bearer plugin-key",
                            "X-Friday-Memory-Auth-Type": "api_key",
                            "Content-Type": "application/json",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "config.toml").write_text(
        "[mcp_servers.memory_connector]\n"
        'url = "https://config.example/mcp"\n'
        "[mcp_servers.memory_connector.http_headers]\n"
        'Authorization = "Bearer config-key"\n',
        encoding="utf-8",
    )
    (root / "memory_connector.env").write_text(
        "MEMORY_CONNECTOR_URL=https://file.example/mcp\n"
        "CONNECTOR_API_KEY=file-key\n"
        "MEMORY_CONNECTOR_USER_ID=must-not-pass\n"
        "UNRELATED_SECRET=must-not-pass\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONNECTOR_API_KEY", "process-key-with-$-and-quotes-'\"")
    monkeypatch.setenv("MEMORY_CONNECTOR_USER_ID", "legacy-user")

    env = memory_connector_env()

    assert env == {
        "MEMORY_CONNECTOR_URL": "https://file.example/mcp",
        "CONNECTOR_API_KEY": "process-key-with-$-and-quotes-'\"",
        "MEMORY_CONNECTOR_AUTH_TYPE": "api_key",
        "MEMORY_CONNECTOR_CONTENT_TYPE": "application/json",
    }


def test_config_and_plugin_parsers_extract_only_reviewed_fields(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.memory_connector]\n"
        'url = "https://memory.example/mcp"\n'
        "[mcp_servers.memory_connector.http_headers]\n"
        'Authorization = "Bearer config-key"\n'
        'X-Friday-Memory-Auth-Type = "api_key"\n'
        'Content-Type = "application/json"\n',
        encoding="utf-8",
    )
    plugin = tmp_path / ".mcp.json"
    plugin.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory_connector": {
                        "url": "https://plugin.example/mcp",
                        "http_headers": {"Authorization": "plugin-key"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert memory_connector_env_from_config(config)["CONNECTOR_API_KEY"] == "config-key"
    assert memory_connector_env_from_plugin_config(plugin) == {
        "MEMORY_CONNECTOR_URL": "https://plugin.example/mcp",
        "CONNECTOR_API_KEY": "plugin-key",
    }


def test_expired_jwt_is_removed_and_reported(isolate_memory_environment: Path):
    token = unsigned_jwt({"exp": 1})
    (isolate_memory_environment / "config.toml").write_text(
        "[mcp_servers.memory_connector]\n"
        'url = "https://memory.example/mcp"\n'
        "[mcp_servers.memory_connector.http_headers]\n"
        f'Authorization = "Bearer {token}"\n',
        encoding="utf-8",
    )

    assert jwt_token_is_expired(token, now=2) is True
    assert "CONNECTOR_API_KEY" not in memory_connector_env()
    assert memory_connector_config_issue() == "memory connector token is expired"


def test_memory_config_requires_url_and_copyable_api_key(
    isolate_memory_environment: Path,
):
    assert memory_connector_config_issue() == "memory connector URL is missing"

    (isolate_memory_environment / "memory_connector.env").write_text(
        "MEMORY_CONNECTOR_URL=https://memory.example/mcp\n",
        encoding="utf-8",
    )
    assert memory_connector_config_issue() == (
        "memory connector API key is missing or expired"
    )
