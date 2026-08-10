from __future__ import annotations

import base64
import json
import os
import shlex
import time
import tomllib
from pathlib import Path


MEMORY_CONNECTOR_ENV_FILE = "memory_connector.env"
MEMORY_CONNECTOR_URL_ENV = "MEMORY_CONNECTOR_URL"
MEMORY_CONNECTOR_API_KEY_ENV = "CONNECTOR_API_KEY"
MEMORY_CONNECTOR_AUTH_TYPE_ENV = "MEMORY_CONNECTOR_AUTH_TYPE"
MEMORY_CONNECTOR_CONTENT_TYPE_ENV = "MEMORY_CONNECTOR_CONTENT_TYPE"
MEMORY_CONNECTOR_ENV_KEYS = {
    MEMORY_CONNECTOR_API_KEY_ENV,
    MEMORY_CONNECTOR_AUTH_TYPE_ENV,
    MEMORY_CONNECTOR_CONTENT_TYPE_ENV,
    MEMORY_CONNECTOR_URL_ENV,
}
MEMORY_CONNECTOR_CONFIG_DIR_ENV = "CEO_MEMORY_CONNECTOR_CONFIG_DIR"


def memory_connector_config_dir() -> Path:
    configured = os.environ.get(MEMORY_CONNECTOR_CONFIG_DIR_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    legacy_codex_home = os.environ.get("CODEX_HOME", "").strip()
    if legacy_codex_home:
        return Path(os.path.expandvars(legacy_codex_home)).expanduser()
    return Path.home() / ".codex"


def parse_export_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        tokens = shlex.split(line, comments=True, posix=True)
        if not tokens:
            continue
        if tokens[0] == "export":
            tokens = tokens[1:]
        for token in tokens:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            values[key] = value
    return values


def memory_connector_env() -> dict[str, str]:
    root = memory_connector_config_dir()
    plugin_env = memory_connector_env_from_plugin_config(
        root / "plugins" / "memory-connector" / ".mcp.json"
    )
    file_env = parse_export_env_file(root / MEMORY_CONNECTOR_ENV_FILE)
    whitelisted_file_env = {
        key: value for key, value in file_env.items() if key in MEMORY_CONNECTOR_ENV_KEYS
    }
    config_env = memory_connector_env_from_config(root / "config.toml")
    env = {**plugin_env, **config_env, **whitelisted_file_env, **os.environ}
    env.pop("MEMORY_CONNECTOR_USER_ID", None)
    token = env.get(MEMORY_CONNECTOR_API_KEY_ENV)
    if token and jwt_token_is_expired(token):
        env.pop(MEMORY_CONNECTOR_API_KEY_ENV, None)
    return {
        key: env[key]
        for key in MEMORY_CONNECTOR_ENV_KEYS
        if isinstance(env.get(key), str) and env[key]
    }


def memory_connector_env_from_config(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    memory_config = (payload.get("mcp_servers") or {}).get("memory_connector") or {}
    if not isinstance(memory_config, dict):
        return {}
    return _memory_connector_env_from_server_config(memory_config)


def memory_connector_env_from_plugin_config(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    memory_config = servers.get("memory_connector") if isinstance(servers, dict) else None
    if not isinstance(memory_config, dict):
        return {}
    return _memory_connector_env_from_server_config(memory_config)


def memory_connector_config_issue() -> str:
    env = memory_connector_env()
    if not env.get(MEMORY_CONNECTOR_URL_ENV):
        return "memory connector URL is missing"
    if env.get(MEMORY_CONNECTOR_API_KEY_ENV):
        return ""
    configured = memory_connector_env_from_config(
        memory_connector_config_dir() / "config.toml"
    ).get(MEMORY_CONNECTOR_API_KEY_ENV)
    if configured and jwt_token_is_expired(configured):
        return "memory connector token is expired"
    return "memory connector API key is missing or expired"


def jwt_token_is_expired(token: str, *, now: float | None = None) -> bool:
    parts = token.split(".")
    if len(parts) < 2:
        return False
    try:
        padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return False
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int | float):
        return False
    return expires_at <= (time.time() if now is None else now)


def _memory_connector_env_from_server_config(
    memory_config: dict[str, object],
) -> dict[str, str]:
    env: dict[str, str] = {}
    url = memory_config.get("url")
    if isinstance(url, str) and url.strip():
        env[MEMORY_CONNECTOR_URL_ENV] = url.strip()
    headers = memory_config.get("http_headers")
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    if isinstance(authorization, str) and authorization.strip():
        token = authorization.strip()
        if token.casefold().startswith("bearer "):
            token = token[7:].strip()
        if token:
            env[MEMORY_CONNECTOR_API_KEY_ENV] = token
    auth_type = (
        headers.get("X-Friday-Memory-Auth-Type")
        if isinstance(headers, dict)
        else None
    )
    if isinstance(auth_type, str) and auth_type.strip():
        env[MEMORY_CONNECTOR_AUTH_TYPE_ENV] = auth_type.strip()
    content_type = headers.get("Content-Type") if isinstance(headers, dict) else None
    if isinstance(content_type, str) and content_type.strip():
        env[MEMORY_CONNECTOR_CONTENT_TYPE_ENV] = content_type.strip()
    return env
