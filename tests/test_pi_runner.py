import json
from pathlib import Path

import pytest

from app.pi_runner import (
    DEFAULT_PI_API,
    DEFAULT_PI_MODEL,
    DEFAULT_PI_PROVIDER,
    PI_API_KEY_ENV,
    PI_MODEL_SOURCE_ENV,
    PiRunner,
    SUPPORTED_PI_APIS,
    ensure_pi_runtime_config,
    normalize_pi_model_selection,
    pi_allowed_read_roots,
    pi_memory_connector_config_issue,
    pi_models_config,
    pi_models_config_for_values,
    pi_process_failure_reason,
    selected_pi_api,
    selected_pi_base_url,
    selected_pi_model_source,
    selected_pi_provider,
)


def test_pi_defaults_prefer_deepseek_while_retaining_openai_protocols():
    assert DEFAULT_PI_PROVIDER == "deepseek"
    assert DEFAULT_PI_MODEL == "deepseek-v4-pro"
    assert DEFAULT_PI_API == "openai-completions"
    assert "openai-completions" in SUPPORTED_PI_APIS
    assert "openai-responses" in SUPPORTED_PI_APIS


def _configure_runtime(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    agent_dir = tmp_path / "pi-agent"
    session_dir = tmp_path / "pi-sessions"
    monkeypatch.setenv("CEO_PI_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("CEO_PI_SESSION_DIR", str(session_dir))
    monkeypatch.setenv("CEO_PI_PROVIDER", "openai")
    monkeypatch.setenv("CEO_PI_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv(PI_MODEL_SOURCE_ENV, "builtin")
    monkeypatch.setenv("CEO_PI_API", "openai-responses")
    monkeypatch.setenv("CEO_PI_BASE_URL", "")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    for key in (
        "MEMORY_CONNECTOR_URL",
        "CONNECTOR_API_KEY",
        "MEMORY_CONNECTOR_AUTH_TYPE",
        "MEMORY_CONNECTOR_CONTENT_TYPE",
        "MEMORY_CONNECTOR_USER_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    return agent_dir, session_dir


def test_pi_runner_builds_json_session_command_without_exposing_api_key(
    tmp_path: Path, monkeypatch
):
    _agent_dir, session_dir = _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CEO_PI_API_KEY", "super-secret-key")
    runner = PiRunner(
        workspace=tmp_path,
        pi_cli_path_value=tmp_path / "pi" / "dist" / "cli.js",
        node_binary="/node22/bin/node",
    )

    command = runner.build_command(
        prompt="hello",
        session_id="session-1",
        developer_instructions="Return JSON.",
    )

    assert command[:2] == [
        "/node22/bin/node",
        str(tmp_path / "pi" / "dist" / "cli.js"),
    ]
    assert command[command.index("--mode") + 1] == "json"
    assert command[command.index("--session-dir") + 1] == str(session_dir)
    assert command[command.index("--provider") + 1] == "openai"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert command[command.index("--thinking") + 1] == "medium"
    assert "--no-builtin-tools" in command
    assert "--no-extensions" in command
    assert command[command.index("--extension") + 1].endswith(
        "pi_extensions/ceo_agent_tools.ts"
    )
    assert command[command.index("--session-id") + 1] == "session-1"
    assert command[command.index("--system-prompt") + 1] == "Return JSON."
    assert "super-secret-key" not in command
    assert "--api-key" not in command


def test_pi_runner_read_only_command_allows_only_read_tools(tmp_path: Path, monkeypatch):
    _configure_runtime(monkeypatch, tmp_path)
    runner = PiRunner(workspace=tmp_path, pi_cli_path_value="pi.js")

    command = runner.build_command(
        prompt="inspect",
        session_id=None,
        approval_policy="never",
    )

    assert command[command.index("--tools") + 1] == (
        "workspace_read,workspace_search,workspace_list,graphify_read,download_dingtalk_image,execute_reviewed_read,"
        "execute_reviewed_lark_read,"
        "user_get,memory_recall,memory_get,timeline_get,web_search_exa,web_fetch_exa,"
        "search_candidates,get_dashboard_stats,get_interview_context,"
        "download_attachment,list_candidate_interviews"
    )


def test_pi_runner_effectful_command_exposes_only_reviewed_extension_tools(
    tmp_path: Path, monkeypatch
):
    _configure_runtime(monkeypatch, tmp_path)
    runner = PiRunner(workspace=tmp_path, pi_cli_path_value="pi.js")

    command = runner.build_command(
        prompt="execute",
        session_id=None,
        approval_policy="untrusted",
    )

    assert command[command.index("--tools") + 1] == (
        "workspace_read,workspace_search,workspace_list,graphify_read,download_dingtalk_image,execute_reviewed_read,"
        "execute_reviewed_lark_read,"
        "user_get,memory_recall,memory_get,timeline_get,web_search_exa,web_fetch_exa,"
        "search_candidates,get_dashboard_stats,get_interview_context,"
        "download_attachment,list_candidate_interviews,"
        "execute_reviewed_write,execute_reviewed_lark_write,"
        "memory_write,document_upload,upload_interview_result"
    )
    assert "bash" not in command[command.index("--tools") + 1].split(",")
    assert "write" not in command[command.index("--tools") + 1].split(",")


def test_pi_runner_profile_distillation_exposes_only_workspace_and_profile_write(
    tmp_path: Path,
    monkeypatch,
):
    _configure_runtime(monkeypatch, tmp_path)
    runner = PiRunner(workspace=tmp_path, pi_cli_path_value="pi.js")

    command = runner.build_command(
        prompt="distill",
        session_id=None,
        approval_policy="never",
        profile_distillation=True,
    )

    assert command[command.index("--tools") + 1] == (
        "workspace_read,workspace_search,workspace_list,write_work_profile"
    )


def test_pi_runner_profile_distillation_rejects_effectful_general_policy(
    tmp_path: Path,
    monkeypatch,
):
    _configure_runtime(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="requires never approval policy"):
        PiRunner(workspace=tmp_path, pi_cli_path_value="pi.js").build_command(
            prompt="distill",
            session_id=None,
            approval_policy="untrusted",
            profile_distillation=True,
        )


def test_pi_runtime_models_config_preserves_builtin_model_metadata(
    tmp_path: Path, monkeypatch
):
    agent_dir, session_dir = _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CEO_PI_BASE_URL", "https://gateway.example/v1/")
    monkeypatch.setenv("CEO_PI_API_KEY", "super-secret-key")

    path = ensure_pi_runtime_config()
    payload = json.loads(path.read_text(encoding="utf-8"))
    provider = payload["providers"]["openai"]

    assert path == agent_dir / "models.json"
    assert provider["baseUrl"] == "https://gateway.example/v1"
    assert provider["apiKey"] == f"${PI_API_KEY_ENV}"
    assert "api" not in provider
    assert "models" not in provider
    assert "super-secret-key" not in path.read_text(encoding="utf-8")
    assert session_dir.is_dir()


def test_legacy_openai_deepseek_responses_config_uses_builtin_deepseek_protocol(
    tmp_path: Path,
    monkeypatch,
):
    agent_dir, _session_dir = _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CEO_PI_PROVIDER", "openai")
    monkeypatch.setenv("CEO_PI_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv(PI_MODEL_SOURCE_ENV, "builtin")
    monkeypatch.setenv("CEO_PI_API", "openai-responses")
    monkeypatch.setenv("CEO_PI_BASE_URL", "https://gateway.example/v1")

    assert selected_pi_provider() == "deepseek"
    assert selected_pi_api() == "openai-completions"
    assert selected_pi_model_source() == "builtin"

    path = ensure_pi_runtime_config()
    provider = json.loads(path.read_text(encoding="utf-8"))["providers"][
        "deepseek"
    ]
    assert path == agent_dir / "models.json"
    assert provider == {
        "apiKey": f"${PI_API_KEY_ENV}",
        "baseUrl": "https://gateway.example/v1",
    }

    command = PiRunner(workspace=tmp_path, pi_cli_path_value="pi.js").build_command(
        prompt="hello",
        session_id=None,
    )
    assert command[command.index("--provider") + 1] == "deepseek"
    assert command[command.index("--model") + 1] == "deepseek-v4-pro"


def test_custom_deepseek_series_model_uses_completions_and_deepseek_compat():
    selection = normalize_pi_model_selection(
        provider="openai",
        model="deepseek-r1-company",
        model_source="builtin",
        api="openai-responses",
        base_url="https://gateway.example/v1",
    )

    assert selection.provider == "deepseek"
    assert selection.api == "openai-completions"
    assert selection.model_source == "custom"

    provider = pi_models_config_for_values(
        provider=selection.provider,
        model=selection.model,
        model_source=selection.model_source,
        api=selection.api,
        base_url=selection.base_url,
    )["providers"]["deepseek"]
    model = provider["models"][0]
    assert provider["api"] == "openai-completions"
    assert model["id"] == "deepseek-r1-company"
    assert model["reasoning"] is True
    assert model["compat"] == {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "requiresReasoningContentOnAssistantMessages": True,
        "thinkingFormat": "deepseek",
    }


def test_yunwu_deepseek_gateway_uses_custom_finish_reason_compat():
    selection = normalize_pi_model_selection(
        provider="deepseek",
        model="deepseek-v4-pro",
        model_source="custom",
        api="openai-responses",
        base_url="https://api3.wlai.vip",
    )

    assert selection.provider == "yunwu"
    assert selection.model_source == "custom"
    assert selection.api == "openai-completions"
    assert selection.base_url == "https://api3.wlai.vip/v1"

    provider = pi_models_config_for_values(
        provider=selection.provider,
        model=selection.model,
        model_source=selection.model_source,
        api=selection.api,
        base_url=selection.base_url,
    )["providers"]["yunwu"]
    assert provider["api"] == "openai-completions"
    assert provider["baseUrl"] == "https://api3.wlai.vip/v1"
    assert provider["models"][0]["compat"] == {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "requiresReasoningContentOnAssistantMessages": True,
        "thinkingFormat": "deepseek",
        "supportsFinishReason": False,
    }


def test_yunwu_full_chat_completions_url_normalizes_to_sdk_base_url():
    selection = normalize_pi_model_selection(
        provider="yunwu",
        model="deepseek-v4-pro",
        model_source="custom",
        api="openai-completions",
        base_url="https://yunwu.ai/v1/chat/completions",
    )

    assert selection.base_url == "https://yunwu.ai/v1"


def test_pi_runtime_models_config_defines_genuinely_custom_model(
    tmp_path: Path, monkeypatch
):
    agent_dir, _session_dir = _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv(PI_MODEL_SOURCE_ENV, "custom")
    monkeypatch.setenv("CEO_PI_PROVIDER", "custom-provider")
    monkeypatch.setenv("CEO_PI_MODEL", "custom-model")
    monkeypatch.setenv("CEO_PI_BASE_URL", "https://gateway.example/v1")

    path = ensure_pi_runtime_config()
    provider = json.loads(path.read_text(encoding="utf-8"))["providers"][
        "custom-provider"
    ]

    assert path == agent_dir / "models.json"
    assert provider["api"] == "openai-responses"
    assert provider["models"] == [{"id": "custom-model", "name": "custom-model"}]


def test_pi_models_config_keeps_builtin_provider_when_base_url_is_empty(
    tmp_path: Path, monkeypatch
):
    _configure_runtime(monkeypatch, tmp_path)

    assert pi_models_config() == {
        "providers": {"openai": {"apiKey": f"${PI_API_KEY_ENV}"}}
    }


def test_pi_base_url_rejects_non_http_urls(monkeypatch):
    monkeypatch.setenv("CEO_PI_BASE_URL", "file:///tmp/provider")

    with pytest.raises(ValueError, match="absolute http"):
        selected_pi_base_url()


def test_pi_runner_environment_uses_isolated_agent_directories(
    tmp_path: Path, monkeypatch
):
    agent_dir, session_dir = _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CEO_PI_API_KEY", "super-secret-key")
    runner = PiRunner(workspace=tmp_path, pi_cli_path_value="pi.js")

    env = runner.build_env()

    assert env["PI_CODING_AGENT_DIR"] == str(agent_dir)
    assert env["PI_CODING_AGENT_SESSION_DIR"] == str(session_dir)
    assert env["PI_OFFLINE"] == "1"
    assert env["CEO_PI_API_KEY"] == "super-secret-key"
    assert env["CEO_PI_PYTHON_BINARY"]
    assert env["CEO_PI_MEMORY_BRIDGE_PATH"].endswith("app/pi_memory_bridge.py")
    assert env["CEO_PI_EXA_BRIDGE_PATH"].endswith("app/pi_exa_bridge.py")
    assert env["CEO_PI_EXA_MCP_URL"] == "https://mcp.exa.ai/mcp"
    assert env["CEO_PI_XIAOQING_BRIDGE_PATH"].endswith(
        "app/pi_xiaoqing_bridge.py"
    )
    assert env["CEO_PI_XIAOQING_MCP_URL"] == (
        "https://interview.hr.startask.net/mcp"
    )


def test_pi_runner_environment_prepends_configured_node_directory(
    tmp_path: Path,
    monkeypatch,
):
    _configure_runtime(monkeypatch, tmp_path)
    node = tmp_path / "node22" / "bin" / "node"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    runner = PiRunner(
        workspace=tmp_path,
        pi_cli_path_value="pi.js",
        node_binary=str(node),
    )

    env = runner.build_env()

    assert env["PATH"].split(":") == [str(node.parent), "/usr/bin", "/bin"]


def test_pi_default_read_roots_do_not_depend_on_codex_skills(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("CEO_PI_ALLOWED_READ_ROOTS", raising=False)
    monkeypatch.setattr("app.pi_runner.Path.home", lambda: tmp_path)

    roots = pi_allowed_read_roots(tmp_path / "workspace")

    assert (tmp_path / ".agents" / "skills").resolve() in roots
    assert (tmp_path / ".codex" / "skills").resolve() not in roots


def test_pi_runner_loads_only_reviewed_memory_connector_environment(
    tmp_path: Path,
    monkeypatch,
):
    _configure_runtime(monkeypatch, tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "memory_connector.env").write_text(
        "\n".join(
            [
                "MEMORY_CONNECTOR_URL=https://memory.example/mcp",
                "CONNECTOR_API_KEY=memory-secret",
                "MEMORY_CONNECTOR_AUTH_TYPE=api_key",
                "MEMORY_CONNECTOR_CONTENT_TYPE=application/json",
                "MEMORY_CONNECTOR_USER_ID=must-not-pass",
                "UNRELATED_SECRET=must-not-load",
            ]
        ),
        encoding="utf-8",
    )

    env = PiRunner(workspace=tmp_path, pi_cli_path_value="pi.js").build_env()

    assert env["MEMORY_CONNECTOR_URL"] == "https://memory.example/mcp"
    assert env["CONNECTOR_API_KEY"] == "memory-secret"
    assert env["MEMORY_CONNECTOR_AUTH_TYPE"] == "api_key"
    assert env["MEMORY_CONNECTOR_CONTENT_TYPE"] == "application/json"
    assert "MEMORY_CONNECTOR_USER_ID" not in env
    assert "UNRELATED_SECRET" not in env
    assert pi_memory_connector_config_issue() == ""


def test_pi_memory_connector_requires_copyable_api_key(tmp_path: Path, monkeypatch):
    _configure_runtime(monkeypatch, tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "memory_connector.env").write_text(
        "MEMORY_CONNECTOR_URL=https://memory.example/mcp\n",
        encoding="utf-8",
    )

    assert pi_memory_connector_config_issue() == (
        "memory connector API key is missing or expired"
    )


def test_pi_process_failure_reason_classifies_connection_error():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "Connection error.",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "auto_retry_end",
                    "success": False,
                    "finalError": "Connection error.",
                }
            ),
        ]
    )

    assert (
        pi_process_failure_reason(stdout)
        == "pi_provider_unavailable: Connection error."
    )


def test_pi_process_failure_reason_classifies_api_key_error():
    stdout = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "401 Unauthorized: invalid API key",
            },
        }
    )

    assert pi_process_failure_reason(stdout) == (
        "pi_provider_auth_failed: 401 Unauthorized: invalid API key"
    )


def test_pi_process_failure_reason_ignores_successful_assistant_text():
    stdout = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "done"}],
            },
        }
    )

    assert pi_process_failure_reason(stdout) == ""


def test_pi_process_failure_reason_requires_settled_lifecycle():
    stdout = "\n".join(
        [
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "agent_end", "willRetry": False}),
        ]
    )

    assert pi_process_failure_reason(stdout) == "pi_not_settled"


def test_pi_process_failure_reason_detects_print_mode_extension_error():
    stdout = "\n".join(
        [
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "agent_settled"}),
        ]
    )
    stderr = "Extension error (/tmp/extension.ts): handler failed\n"

    assert pi_process_failure_reason(stdout, stderr) == "pi_extension_failed"


def test_pi_process_failure_reason_prioritizes_provider_error_over_not_settled():
    stdout = "\n".join(
        [
            json.dumps({"type": "agent_start"}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "401 Unauthorized: invalid API key",
                    },
                }
            ),
        ]
    )

    assert pi_process_failure_reason(stdout) == (
        "pi_provider_auth_failed: 401 Unauthorized: invalid API key"
    )
