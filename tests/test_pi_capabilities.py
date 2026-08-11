from pathlib import Path

import pytest

from app.pi_capabilities import (
    _reviewed_lark_status,
    _safe_probe_environment,
    _valid_external_mcp_url,
    _valid_memory_url,
    probe_pi_capabilities,
    probe_pi_model_resolution,
)
from app.pi_runner import (
    GRAPHIFY_BINARY_ENV,
    PI_API_ENV,
    PI_API_KEY_ENV,
    PI_BASE_URL_ENV,
    PI_CLI_PATH_ENV,
    PI_EXTENSION_PATH_ENV,
    PI_EXA_BRIDGE_PATH_ENV,
    PI_EXA_MCP_URL_ENV,
    PI_XIAOQING_ACCESS_TOKEN_ENV,
    PI_XIAOQING_BRIDGE_PATH_ENV,
    PI_XIAOQING_MCP_URL_ENV,
    PI_MEMORY_BRIDGE_PATH_ENV,
    PI_MODEL_ENV,
    PI_NODE_BINARY_ENV,
    PI_PROVIDER_ENV,
    pi_cli_path,
    pi_node_binary,
)


def _runtime_paths(tmp_path: Path) -> dict[str, str]:
    cli = tmp_path / "pi" / "dist" / "cli.js"
    loader = cli.parent / "core" / "extensions" / "loader.js"
    extension = tmp_path / "ceo_agent_tools.ts"
    bridge = tmp_path / "pi_memory_bridge.py"
    exa_bridge = tmp_path / "pi_exa_bridge.py"
    xiaoqing_bridge = tmp_path / "pi_xiaoqing_bridge.py"
    loader.parent.mkdir(parents=True)
    cli.write_text("// cli", encoding="utf-8")
    loader.write_text("// loader", encoding="utf-8")
    extension.write_text("// extension", encoding="utf-8")
    bridge.write_text("# bridge", encoding="utf-8")
    exa_bridge.write_text("# bridge", encoding="utf-8")
    xiaoqing_bridge.write_text("# bridge", encoding="utf-8")
    return {
        PI_NODE_BINARY_ENV: str(tmp_path / "node"),
        PI_CLI_PATH_ENV: str(cli),
        PI_EXTENSION_PATH_ENV: str(extension),
        PI_MEMORY_BRIDGE_PATH_ENV: str(bridge),
        PI_EXA_BRIDGE_PATH_ENV: str(exa_bridge),
        PI_EXA_MCP_URL_ENV: "https://mcp.exa.ai/mcp",
        PI_XIAOQING_BRIDGE_PATH_ENV: str(xiaoqing_bridge),
        PI_XIAOQING_MCP_URL_ENV: "https://interview.hr.startask.net/mcp",
        PI_XIAOQING_ACCESS_TOKEN_ENV: "xiaoqing-secret",
        PI_PROVIDER_ENV: "custom-provider",
        PI_MODEL_ENV: "custom-model",
        PI_API_ENV: "openai-responses",
        PI_BASE_URL_ENV: "https://gateway.example/v1",
        PI_API_KEY_ENV: "provider-secret",
        GRAPHIFY_BINARY_ENV: str(tmp_path / "bin" / "graphify"),
    }


def test_capability_report_uses_real_reviewed_boundaries_without_echoing_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_values = _runtime_paths(tmp_path)
    monkeypatch.setattr(
        "app.pi_capabilities.pi_node_version",
        lambda _binary: (22, 22, 2),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._load_extension_cached",
        lambda *_args: (True, ""),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_dws_status",
        lambda: (True, "Installed schema: 10 read, 4 write tools"),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_lark_status",
        lambda _binary, _node_binary: (True, "Official schema ready"),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_graphify_status",
        lambda _binary: (True, "Graphify ready"),
    )
    monkeypatch.setattr(
        "app.pi_capabilities.probe_pi_model_resolution",
        lambda **_kwargs: (True, "Resolved offline"),
    )
    nvwa = tmp_path / "nvwa" / "SKILL.md"
    nvwa.parent.mkdir()
    nvwa.write_text("# Nvwa", encoding="utf-8")
    monkeypatch.setattr("app.pi_capabilities.nvwa_skill_path", lambda: nvwa)

    report = probe_pi_capabilities(
        env_values=env_values,
        root=tmp_path,
        memory_env={
            "MEMORY_CONNECTOR_URL": "https://memory.example/mcp",
            "CONNECTOR_API_KEY": "memory-secret",
        },
    )

    assert report.runtime_ready is True
    assert report.get("reviewed_extension").ready is True
    assert report.get("dws_reviewed_tools").ready is True
    assert report.get("graphify").ready is True
    assert report.get("memory_tools").ready is True
    assert report.get("xiaoqing_interview").state == "ready"
    assert report.get("xiaoqing_interview").ready is True
    assert report.get("exa").state == "ready"
    assert report.get("exa").ready is True
    assert report.get("lark").state == "ready"
    assert report.get("lark").ready is True
    assert report.get("nvwa").ready is True
    assert report.integrations_ready is True
    assert report.full_stack_ready is True
    assert report.as_dict()["full_stack_ready"] is True
    serialized = str(report.as_dict())
    assert "provider-secret" not in serialized
    assert "memory-secret" not in serialized
    assert "xiaoqing-secret" not in serialized


def test_capability_report_requires_provider_key_but_not_optional_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_values = _runtime_paths(tmp_path)
    env_values[PI_API_KEY_ENV] = ""
    monkeypatch.setattr(
        "app.pi_capabilities.pi_node_version",
        lambda _binary: (22, 22, 2),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._load_extension_cached",
        lambda *_args: (True, ""),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_dws_status",
        lambda: (True, "schema ready"),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_lark_status",
        lambda _binary, _node_binary: (
            False,
            "lark-cli executable is not installed",
        ),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_graphify_status",
        lambda _binary: (False, "graphify executable is not installed"),
    )
    monkeypatch.setattr(
        "app.pi_capabilities.probe_pi_model_resolution",
        lambda **_kwargs: (True, "Resolved offline"),
    )

    report = probe_pi_capabilities(
        env_values=env_values,
        root=tmp_path,
        memory_env={},
    )

    assert report.runtime_ready is False
    assert report.get("provider_api_key").state == "missing_config"
    assert report.get("memory_bridge").ready is True
    assert report.get("memory_tools").state == "missing_config"
    assert report.full_stack_ready is False


def test_capability_subprocess_environment_strips_all_provider_and_memory_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CEO_PI_API_KEY", "pi-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("CONNECTOR_API_KEY", "memory-secret")
    monkeypatch.setenv("AUTHORIZATION", "Bearer secret")
    monkeypatch.setenv("PROVIDER_CLIENT_SECRET", "provider-secret")
    monkeypatch.setenv("SAFE_MARKER", "kept")

    env = _safe_probe_environment("node")

    assert env["PATH"] == "/usr/bin"
    assert env["SAFE_MARKER"] == "kept"
    assert "CEO_PI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CONNECTOR_API_KEY" not in env
    assert "AUTHORIZATION" not in env
    assert "PROVIDER_CLIENT_SECRET" not in env


def test_lark_schema_probe_adds_configured_node_directory_to_sanitized_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_dir = tmp_path / "node22" / "bin"
    node_dir.mkdir(parents=True)
    node = node_dir / "node"
    node.write_text("#!/bin/sh\nexec /bin/sh \"$@\"\n", encoding="utf-8")
    node.chmod(0o755)
    lark = tmp_path / "lark-cli"
    lark.write_text(
        "#!/usr/bin/env node\n"
        "printf '%s\\n' '[{\"_meta\":{\"risk\":\"read\"}}]'\n",
        encoding="utf-8",
    )
    lark.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("CEO_PI_API_KEY", "must-not-leak")

    ready, detail = _reviewed_lark_status(str(lark), str(node))

    assert ready is True
    assert "Official schema: 1 read" in detail


def test_capability_report_blocks_unresolvable_provider_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_values = _runtime_paths(tmp_path)
    monkeypatch.setattr(
        "app.pi_capabilities.pi_node_version",
        lambda _binary: (22, 22, 2),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._load_extension_cached",
        lambda *_args: (True, ""),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_dws_status",
        lambda: (True, "schema ready"),
    )
    monkeypatch.setattr(
        "app.pi_capabilities._reviewed_lark_status",
        lambda _binary, _node_binary: (True, "schema ready"),
    )
    monkeypatch.setattr(
        "app.pi_capabilities.probe_pi_model_resolution",
        lambda **_kwargs: (
            False,
            "Pi model configuration is invalid: unknown provider",
        ),
    )

    report = probe_pi_capabilities(
        env_values=env_values,
        root=tmp_path,
        memory_env={},
    )

    assert report.runtime_ready is False
    assert report.get("provider").ready is False
    assert "unknown provider" in report.get("provider").detail


def test_capability_urls_allow_http_only_for_loopback():
    assert _valid_memory_url("https://memory.example/mcp") is True
    assert _valid_memory_url("http://127.0.0.1:9999/mcp") is True
    assert _valid_memory_url("http://memory.example/mcp") is False
    assert _valid_external_mcp_url("https://mcp.example/mcp") is True
    assert _valid_external_mcp_url("http://localhost:9999/mcp") is True
    assert _valid_external_mcp_url("http://mcp.example/mcp") is False


def test_model_probe_rejects_pi_custom_id_fallback_as_builtin():
    ready, detail = probe_pi_model_resolution(
        node_binary=pi_node_binary(),
        cli_path=pi_cli_path(),
        provider="openai",
        model="deepseek-v4-pro",
        model_source="builtin",
        api="openai-responses",
        base_url="https://gateway.example/v1",
    )

    assert ready is False
    assert "Using custom model id" in detail

    custom_ready, custom_detail = probe_pi_model_resolution(
        node_binary=pi_node_binary(),
        cli_path=pi_cli_path(),
        provider="openai",
        model="deepseek-v4-pro",
        model_source="custom",
        api="openai-responses",
        base_url="https://gateway.example/v1",
    )

    assert custom_ready is True
    assert "custom model metadata" in custom_detail
