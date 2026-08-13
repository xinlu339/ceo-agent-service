import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.config import repo_root, work_profile_path
from app.dws_client import dws_noninteractive_environment
from app.pi_events import pi_stream_completion_issue


PI_NODE_BINARY_ENV = "CEO_PI_NODE_BINARY"
PI_CLI_PATH_ENV = "CEO_PI_CLI_PATH"
PI_PROVIDER_ENV = "CEO_PI_PROVIDER"
PI_MODEL_ENV = "CEO_PI_MODEL"
PI_MODEL_SOURCE_ENV = "CEO_PI_MODEL_SOURCE"
PI_API_ENV = "CEO_PI_API"
PI_BASE_URL_ENV = "CEO_PI_BASE_URL"
PI_API_KEY_ENV = "CEO_PI_API_KEY"
PI_THINKING_LEVEL_ENV = "CEO_PI_THINKING_LEVEL"
PI_ROUTINE_THINKING_LEVEL_ENV = "CEO_PI_ROUTINE_THINKING_LEVEL"
PI_AGENT_DIR_ENV = "CEO_PI_AGENT_DIR"
PI_SESSION_DIR_ENV = "CEO_PI_SESSION_DIR"
PI_EXTENSION_PATH_ENV = "CEO_PI_EXTENSION_PATH"
PI_ALLOWED_READ_ROOTS_ENV = "CEO_PI_ALLOWED_READ_ROOTS"
PI_PYTHON_BINARY_ENV = "CEO_PI_PYTHON_BINARY"
PI_MEMORY_BRIDGE_PATH_ENV = "CEO_PI_MEMORY_BRIDGE_PATH"
PI_EXA_MCP_URL_ENV = "CEO_PI_EXA_MCP_URL"
PI_EXA_BRIDGE_PATH_ENV = "CEO_PI_EXA_BRIDGE_PATH"
PI_XIAOQING_MCP_URL_ENV = "CEO_PI_XIAOQING_MCP_URL"
PI_XIAOQING_ACCESS_TOKEN_ENV = "CEO_PI_XIAOQING_ACCESS_TOKEN"
PI_XIAOQING_BRIDGE_PATH_ENV = "CEO_PI_XIAOQING_BRIDGE_PATH"
PI_DINGTALK_IMAGE_BRIDGE_PATH_ENV = "CEO_PI_DINGTALK_IMAGE_BRIDGE_PATH"
PI_WORK_PROFILE_PATH_ENV = "CEO_PI_WORK_PROFILE_PATH"
PI_REPLY_AT_OPEN_DINGTALK_ID_ENV = "CEO_PI_REPLY_AT_OPEN_DINGTALK_ID"
PI_REPLY_SINGLE_CHAT_ENV = "CEO_PI_REPLY_SINGLE_CHAT"
PI_TODO_TRIGGER_SENDER_NAME_ENV = "CEO_PI_TODO_TRIGGER_SENDER_NAME"
PI_TODO_TRIGGER_SENDER_USER_ID_ENV = "CEO_PI_TODO_TRIGGER_SENDER_USER_ID"
PI_TODO_TRIGGER_TEXT_ENV = "CEO_PI_TODO_TRIGGER_TEXT"
PI_TODO_TRIGGER_CREATE_TIME_ENV = "CEO_PI_TODO_TRIGGER_CREATE_TIME"
GRAPHIFY_BINARY_ENV = "CEO_GRAPHIFY_BINARY"

DEFAULT_PI_PROVIDER = "deepseek"
DEFAULT_PI_MODEL = "deepseek-v4-pro"
DEFAULT_PI_MODEL_SOURCE = "builtin"
DEFAULT_PI_API = "openai-completions"
DEFAULT_PI_THINKING_LEVEL = "medium"
DEFAULT_PI_ROUTINE_THINKING_LEVEL = "off"
DEFAULT_PI_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
DEFAULT_PI_XIAOQING_MCP_URL = "https://interview.hr.startask.net/mcp"
SUPPORTED_PI_APIS = frozenset(
    {
        "anthropic-messages",
        "google-generative-ai",
        "openai-completions",
        "openai-responses",
    }
)
SUPPORTED_PI_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)
SUPPORTED_PI_MODEL_SOURCES = frozenset({"builtin", "custom"})
READ_ONLY_PI_TOOLS = (
    "workspace_read",
    "workspace_search",
    "workspace_list",
    "graphify_read",
    "download_dingtalk_image",
    "execute_reviewed_read",
    "execute_reviewed_lark_read",
    "user_get",
    "memory_recall",
    "memory_get",
    "timeline_get",
    "web_search_exa",
    "web_fetch_exa",
    "search_candidates",
    "get_dashboard_stats",
    "get_interview_context",
    "download_attachment",
    "list_candidate_interviews",
)
EFFECTFUL_PI_TOOLS = (
    "execute_reviewed_write",
    "execute_reviewed_lark_write",
    "memory_write",
    "document_upload",
    "upload_interview_result",
)
MEMORY_WRITE_PI_TOOLS = (
    "memory_write",
    "document_upload",
)
PROFILE_DISTILLATION_PI_TOOLS = (
    "workspace_read",
    "workspace_search",
    "workspace_list",
    "write_work_profile",
)
MINIMUM_PI_NODE_VERSION = (22, 19, 0)
_PI_PROVIDER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_NODE_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_DEEPSEEK_BUILTIN_MODELS = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
)
_YUNWU_PROVIDER = "yunwu"
_YUNWU_GATEWAY_HOSTS = frozenset({"api3.wlai.vip", "yunwu.ai"})

# Friendly aliases accepted in environment variables and the configuration
# form.  The canonical IDs are Pi's own provider IDs, so built-in model
# resolution continues to use Pi's maintained provider catalog.
PI_DOMESTIC_PROVIDER_ALIASES = {
    "qwen": "qwen-token-plan-cn",
    "glm": "zai-coding-cn",
    "kimi": "moonshotai-cn",
}

# Stable examples used by the configuration UI/documentation.  The picker still
# reads the sibling Pi catalog, so newly released models appear automatically.
PI_DOMESTIC_MODEL_PRESETS = (
    {
        "label": "通义千问",
        "provider": "qwen-token-plan-cn",
        "model": "qwen3.7-plus",
        "api": "openai-completions",
    },
    {
        "label": "智谱 GLM",
        "provider": "zai-coding-cn",
        "model": "glm-5.2",
        "api": "openai-completions",
    },
    {
        "label": "Kimi",
        "provider": "moonshotai-cn",
        "model": "kimi-k2.6",
        "api": "openai-completions",
    },
)

# Fallback compatibility for a manually configured custom endpoint.  For a
# Pi-built-in model we copy the complete metadata from Pi's catalog below; these
# profiles cover a custom model ID where that catalog has no entry yet.
_DOMESTIC_COMPAT_PROFILES: dict[str, dict[str, object]] = {
    "qwen-token-plan": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "thinkingFormat": "qwen",
    },
    "qwen-token-plan-cn": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "thinkingFormat": "qwen",
    },
    "zai": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "thinkingFormat": "zai",
        "zaiToolStream": True,
    },
    "zai-coding-cn": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "thinkingFormat": "zai",
        "zaiToolStream": True,
    },
    "moonshotai": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "supportsStrictMode": False,
        "thinkingFormat": "deepseek",
    },
    "moonshotai-cn": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "supportsStrictMode": False,
        "thinkingFormat": "deepseek",
    },
}


@dataclass(frozen=True)
class PiModelSelection:
    provider: str
    model: str
    model_source: str
    api: str
    base_url: str


def pi_memory_connector_config_issue() -> str:
    env = pi_memory_connector_env()
    if not env.get("MEMORY_CONNECTOR_URL"):
        return "memory connector URL is missing"
    if not env.get("CONNECTOR_API_KEY"):
        return "memory connector API key is missing or expired"
    if not pi_memory_bridge_path().is_file():
        return "Pi Memory Connector bridge is missing"
    return ""


def pi_memory_connector_env() -> dict[str, str]:
    from app.memory_connector_config import memory_connector_env

    source = memory_connector_env()
    return {
        key: source[key]
        for key in {
            "MEMORY_CONNECTOR_URL",
            "CONNECTOR_API_KEY",
            "MEMORY_CONNECTOR_AUTH_TYPE",
            "MEMORY_CONNECTOR_CONTENT_TYPE",
        }
        if source.get(key)
    }


def pi_process_failure_reason(stdout: str, stderr: str = "") -> str:
    error_message = _pi_final_error_message(stdout)
    if error_message:
        normalized = error_message.casefold()
        if any(
            marker in normalized
            for marker in ("401", "403", "api key", "authentication", "unauthorized")
        ):
            prefix = "pi_provider_auth_failed"
        elif any(
            marker in normalized
            for marker in ("connection", "network", "timeout", "fetch failed", "econn")
        ):
            prefix = "pi_provider_unavailable"
        else:
            prefix = "pi_process_failed"
        detail = " ".join(error_message.split())[:1000]
        return f"{prefix}: {detail}"
    if _pi_extension_failed_on_stderr(stderr):
        return "pi_extension_failed"
    return pi_stream_completion_issue(stdout)


def _pi_final_error_message(raw: str) -> str:
    final_error = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "auto_retry_end" and payload.get("success") is False:
            value = payload.get("finalError")
            if isinstance(value, str) and value.strip():
                final_error = value.strip()
            continue
        if payload.get("type") not in {"message_end", "turn_end"}:
            continue
        message = payload.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("stopReason") not in {"error", "aborted"}:
            continue
        value = message.get("errorMessage")
        if isinstance(value, str) and value.strip():
            final_error = value.strip()
    return final_error


def _pi_extension_failed_on_stderr(raw: str) -> bool:
    return any(
        line.lstrip().startswith("Extension error (") for line in raw.splitlines()
    )


def pi_node_binary() -> str:
    configured = os.environ.get(PI_NODE_BINARY_ENV, "").strip()
    if configured:
        return str(Path(os.path.expandvars(configured)).expanduser())
    candidates: list[str] = []
    path_node = shutil.which("node")
    if path_node:
        candidates.append(path_node)
    nvm_versions = Path.home() / ".nvm" / "versions" / "node"
    if nvm_versions.is_dir():
        candidates.extend(
            str(path)
            for path in sorted(nvm_versions.glob("v*/bin/node"), reverse=True)
        )
    for candidate in dict.fromkeys(candidates):
        version = pi_node_version(candidate)
        if version is not None and version >= MINIMUM_PI_NODE_VERSION:
            return candidate
    return path_node or "node"


def pi_node_version(binary: str | Path) -> tuple[int, int, int] | None:
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    match = _NODE_VERSION_PATTERN.match(completed.stdout.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def pi_runtime_environment(
    base_env: dict[str, str] | None = None,
    *,
    node_binary: str | Path | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    resolved_node = Path(node_binary or pi_node_binary()).expanduser()
    if not resolved_node.is_absolute():
        return env
    node_dir = str(resolved_node.parent)
    path_entries = [
        entry for entry in env.get("PATH", "").split(os.pathsep) if entry
    ]
    env["PATH"] = os.pathsep.join(
        [node_dir, *(entry for entry in path_entries if entry != node_dir)]
    )
    return env


def pi_cli_path() -> Path:
    configured = os.environ.get(PI_CLI_PATH_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return (
        repo_root().parent
        / "pi"
        / "packages"
        / "coding-agent"
        / "dist"
        / "cli.js"
    )


def pi_agent_dir() -> Path:
    configured = os.environ.get(PI_AGENT_DIR_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "ceo-agent-service"
        / "pi-agent"
    )


def pi_session_dir() -> Path:
    configured = os.environ.get(PI_SESSION_DIR_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "ceo-agent-service"
        / "pi-sessions"
    )


def pi_extension_path() -> Path:
    configured = os.environ.get(PI_EXTENSION_PATH_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return repo_root() / "pi_extensions" / "ceo_agent_tools.ts"


def pi_python_binary() -> str:
    configured = os.environ.get(PI_PYTHON_BINARY_ENV, "").strip()
    if configured:
        return str(Path(os.path.expandvars(configured)).expanduser())
    return sys.executable


def pi_memory_bridge_path() -> Path:
    configured = os.environ.get(PI_MEMORY_BRIDGE_PATH_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return repo_root() / "app" / "pi_memory_bridge.py"


def pi_exa_bridge_path() -> Path:
    configured = os.environ.get(PI_EXA_BRIDGE_PATH_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return repo_root() / "app" / "pi_exa_bridge.py"


def pi_xiaoqing_bridge_path() -> Path:
    configured = os.environ.get(PI_XIAOQING_BRIDGE_PATH_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return repo_root() / "app" / "pi_xiaoqing_bridge.py"


def pi_dingtalk_image_bridge_path() -> Path:
    configured = os.environ.get(PI_DINGTALK_IMAGE_BRIDGE_PATH_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return repo_root() / "app" / "pi_dingtalk_image_bridge.py"


def pi_allowed_read_roots(workspace: Path) -> tuple[Path, ...]:
    configured = os.environ.get(PI_ALLOWED_READ_ROOTS_ENV, "").strip()
    if configured:
        candidates = [
            Path(os.path.expandvars(value)).expanduser()
            for value in configured.split(os.pathsep)
            if value.strip()
        ]
    else:
        candidates = [
            workspace,
            repo_root(),
            work_profile_path().parent,
            Path.home() / ".agents" / "skills",
        ]
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def selected_pi_provider() -> str:
    return selected_pi_model_selection().provider


def validate_pi_provider(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_PROVIDER
    if not _PI_PROVIDER_PATTERN.fullmatch(value):
        raise ValueError("CEO_PI_PROVIDER contains unsupported characters")
    return value


def selected_pi_model() -> str:
    return selected_pi_model_selection().model


def validate_pi_model(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_MODEL
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("CEO_PI_MODEL must be one non-empty model id")
    return value


def selected_pi_model_source() -> str:
    return selected_pi_model_selection().model_source


def validate_pi_model_source(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_MODEL_SOURCE
    if value not in SUPPORTED_PI_MODEL_SOURCES:
        raise ValueError(f"unsupported Pi model source: {value}")
    return value


def selected_pi_api() -> str:
    return selected_pi_model_selection().api


def validate_pi_api(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_API
    if value not in SUPPORTED_PI_APIS:
        raise ValueError(f"unsupported Pi API protocol: {value}")
    return value


def selected_pi_thinking_level() -> str:
    return validate_pi_thinking_level(
        os.environ.get(PI_THINKING_LEVEL_ENV, DEFAULT_PI_THINKING_LEVEL)
    )


def selected_pi_routine_thinking_level() -> str:
    """Return the cheaper thinking level used for short, self-contained tasks.

    The main thinking level remains the operator's quality/safety setting.  A
    short ordinary reply does not need a full reasoning pass, so it uses a
    separate opt-in setting whose safe default is ``off``.  This is especially
    important for gateways/models that silently clamp ``medium`` to a higher
    supported reasoning level.
    """

    return validate_pi_thinking_level(
        os.environ.get(
            PI_ROUTINE_THINKING_LEVEL_ENV,
            DEFAULT_PI_ROUTINE_THINKING_LEVEL,
        )
    )


def validate_pi_thinking_level(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_THINKING_LEVEL
    if value not in SUPPORTED_PI_THINKING_LEVELS:
        raise ValueError(f"unsupported Pi thinking level: {value}")
    return value


def selected_pi_base_url() -> str:
    return selected_pi_model_selection().base_url


def validate_pi_base_url(raw_value: str) -> str:
    value = raw_value.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("CEO_PI_BASE_URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "CEO_PI_BASE_URL must not contain credentials, a query, or a fragment"
        )
    if parsed.scheme == "http" and not _loopback_hostname(parsed.hostname or ""):
        raise ValueError(
            "CEO_PI_BASE_URL must use HTTPS unless it targets localhost"
        )
    return value


def _is_yunwu_gateway(base_url: str) -> bool:
    if not base_url:
        return False
    hostname = urlparse(base_url).hostname or ""
    return hostname.casefold().rstrip(".") in _YUNWU_GATEWAY_HOSTS


def _normalize_yunwu_base_url(base_url: str) -> str:
    """Return the SDK base URL for Yunwu's OpenAI-compatible endpoint."""

    if not _is_yunwu_gateway(base_url):
        return base_url
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if path in {"", "/v1/chat/completions"}:
        return parsed._replace(path="/v1").geturl()
    return base_url


def _canonical_pi_provider(provider: str) -> str:
    return PI_DOMESTIC_PROVIDER_ALIASES.get(provider.casefold(), provider)


def _builtin_model_metadata(provider: str, model: str) -> dict[str, object] | None:
    """Load Pi's model metadata lazily to avoid a module import cycle."""

    candidates = [provider]
    # DeepSeek is intentionally represented as ``yunwu`` when it is reached
    # through the Yunwu proxy, but its model metadata lives under Pi's built-in
    # DeepSeek provider.
    if (
        provider.casefold() == _YUNWU_PROVIDER
        and model.casefold().rsplit("/", 1)[-1].startswith("deepseek")
    ):
        candidates.append("deepseek")
    try:
        from app.pi_model_catalog import pi_builtin_model_metadata

        for candidate in candidates:
            metadata = pi_builtin_model_metadata(pi_cli_path(), candidate, model)
            if metadata is not None:
                return metadata
    except (ImportError, OSError, ValueError):
        return None
    return None


def normalize_pi_model_selection(
    *,
    provider: str,
    model: str,
    model_source: str = "",
    api: str,
    base_url: str,
) -> PiModelSelection:
    """Normalize legacy DeepSeek settings to Pi's supported wire protocol."""

    provider = _canonical_pi_provider(validate_pi_provider(provider))
    model = validate_pi_model(model)
    api = validate_pi_api(api)
    base_url = validate_pi_base_url(base_url)
    base_url = _normalize_yunwu_base_url(base_url)
    source = (
        validate_pi_model_source(model_source)
        if model_source.strip()
        else "custom"
        if base_url
        else DEFAULT_PI_MODEL_SOURCE
    )

    model_key = model.casefold()
    unqualified_model_key = model_key.rsplit("/", 1)[-1]
    deepseek_model = unqualified_model_key.startswith("deepseek")
    yunwu_gateway = _is_yunwu_gateway(base_url)
    yunwu_provider = provider.casefold() == _YUNWU_PROVIDER
    if deepseek_model and (yunwu_gateway or yunwu_provider):
        provider = _YUNWU_PROVIDER
        api = "openai-completions"
        source = "custom"
    elif deepseek_model and provider.casefold() == "openai":
        provider = "deepseek"
    if provider.casefold() == "deepseek":
        api = "openai-completions"
        source = (
            "builtin"
            if model_key in _DEEPSEEK_BUILTIN_MODELS
            else "custom"
        )
    elif provider.casefold() in _DOMESTIC_COMPAT_PROFILES:
        # Pi's Qwen, GLM and Kimi provider catalogs all use the OpenAI
        # Chat-Completions wire protocol.  Normalize legacy/mistyped API values
        # so a domestic model cannot accidentally be launched through the
        # Responses adapter.
        api = "openai-completions"

    return PiModelSelection(
        provider=provider,
        model=model,
        model_source=source,
        api=api,
        base_url=base_url,
    )


def selected_pi_model_selection() -> PiModelSelection:
    return normalize_pi_model_selection(
        provider=os.environ.get(PI_PROVIDER_ENV, DEFAULT_PI_PROVIDER),
        model=os.environ.get(PI_MODEL_ENV, DEFAULT_PI_MODEL),
        model_source=os.environ.get(PI_MODEL_SOURCE_ENV, ""),
        api=os.environ.get(PI_API_ENV, DEFAULT_PI_API),
        base_url=os.environ.get(PI_BASE_URL_ENV, ""),
    )


def pi_models_config() -> dict[str, object]:
    selection = selected_pi_model_selection()
    return pi_models_config_for_values(
        provider=selection.provider,
        model=selection.model,
        model_source=selection.model_source,
        api=selection.api,
        base_url=selection.base_url,
    )


def pi_models_config_for_values(
    *,
    provider: str,
    model: str,
    model_source: str = "custom",
    api: str,
    base_url: str,
) -> dict[str, object]:
    provider = _canonical_pi_provider(validate_pi_provider(provider))
    model = validate_pi_model(model)
    model_source = validate_pi_model_source(model_source)
    api = validate_pi_api(api)
    base_url = validate_pi_base_url(base_url)
    provider_config: dict[str, object] = {
        "apiKey": f"${PI_API_KEY_ENV}",
    }
    if base_url:
        provider_config["baseUrl"] = base_url
    if model_source == "custom" and (
        base_url or provider.casefold() == "deepseek"
    ):
        model_config: dict[str, object] = {
            "id": model,
            "name": model,
        }
        metadata = _builtin_model_metadata(provider, model)
        if metadata is not None:
            # Only copy fields accepted by Pi's custom model definition.  The
            # provider/base URL remain controlled by the values supplied here.
            if metadata.get("name"):
                model_config["name"] = str(metadata["name"])
            for key in (
                "reasoning",
                "input",
                "contextWindow",
                "maxTokens",
                "compat",
                "thinkingLevelMap",
                "cost",
            ):
                if key in metadata:
                    model_config[key] = metadata[key]

        provider_key = provider.casefold()
        compat: dict[str, object] = {}
        profile = _DOMESTIC_COMPAT_PROFILES.get(provider_key)
        if profile:
            compat.update(profile)
        metadata_compat = model_config.get("compat")
        if isinstance(metadata_compat, dict):
            compat.update(metadata_compat)
        deepseek_custom = provider_key == "deepseek" or (
            provider_key == _YUNWU_PROVIDER
            and model.casefold().rsplit("/", 1)[-1].startswith("deepseek")
        )
        if deepseek_custom:
            compat.update(
                {
                    "supportsStore": False,
                    "supportsDeveloperRole": False,
                    "requiresReasoningContentOnAssistantMessages": True,
                    "thinkingFormat": "deepseek",
                }
            )
            if (
                api == "openai-completions"
                and (
                    provider_key == _YUNWU_PROVIDER
                    or _is_yunwu_gateway(base_url)
                )
            ):
                compat["supportsFinishReason"] = False
            model_config.setdefault("reasoning", True)
            model_config.setdefault("input", ["text"])
        elif profile:
            # Known domestic reasoning providers use text reasoning by default
            # when a custom model ID has no Pi catalog entry.  Built-in model
            # metadata above can override this with its exact capabilities.
            model_config.setdefault("reasoning", True)
            model_config.setdefault("input", ["text"])
        if compat:
            model_config["compat"] = compat
        provider_config.update(
            {
                "api": api,
                "models": [model_config],
            }
        )
    return {"providers": {provider: provider_config}}


def _loopback_hostname(hostname: str) -> bool:
    normalized = hostname.casefold().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def ensure_pi_runtime_config() -> Path:
    agent_dir = pi_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        agent_dir.chmod(0o700)
    except OSError:
        pass
    models_path = agent_dir / "models.json"
    serialized = json.dumps(
        pi_models_config(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if not models_path.exists() or models_path.read_text(encoding="utf-8") != serialized:
        models_path.write_text(serialized, encoding="utf-8")
    try:
        models_path.chmod(0o600)
    except OSError:
        pass
    session_dir = pi_session_dir()
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        session_dir.chmod(0o700)
    except OSError:
        pass
    return models_path


class PiRunner:
    def __init__(
        self,
        workspace: Path,
        pi_cli_path_value: Path | str | None = None,
        node_binary: str | None = None,
    ):
        self.workspace = workspace
        self.pi_cli_path = Path(pi_cli_path_value) if pi_cli_path_value else pi_cli_path()
        self.node_binary = node_binary or pi_node_binary()

    def build_env(
        self,
        *,
        preserve_local_cli_auth: bool = False,
    ) -> dict[str, str]:
        ensure_pi_runtime_config()
        base_env = pi_runtime_environment(node_binary=self.node_binary)
        env = (
            base_env
            if preserve_local_cli_auth
            else dws_noninteractive_environment(base_env)
        )
        if preserve_local_cli_auth:
            env.pop("DINGTALK_DWS_AGENTCODE", None)
            env.pop("CEO_DWS_AGENT_CODE", None)
        for key in {
            "DWS_CLIENT_ID",
            "DWS_CLIENT_SECRET",
            "DINGTALK_APP_KEY",
            "DINGTALK_APP_SECRET",
        }:
            env.pop(key, None)
        env.pop("MEMORY_CONNECTOR_USER_ID", None)
        # These are set per Direct Agent invocation, never inherited from the
        # launchd/service environment.  The reviewed DWS extension uses them
        # only to enforce the original trigger @ on a group native reply.
        env.pop(PI_REPLY_AT_OPEN_DINGTALK_ID_ENV, None)
        env.pop(PI_REPLY_SINGLE_CHAT_ENV, None)
        env.pop(PI_TODO_TRIGGER_SENDER_NAME_ENV, None)
        env.pop(PI_TODO_TRIGGER_SENDER_USER_ID_ENV, None)
        env.pop(PI_TODO_TRIGGER_TEXT_ENV, None)
        env.pop(PI_TODO_TRIGGER_CREATE_TIME_ENV, None)
        env.update(pi_memory_connector_env())
        env["PI_CODING_AGENT_DIR"] = str(pi_agent_dir())
        env["PI_CODING_AGENT_SESSION_DIR"] = str(pi_session_dir())
        env["PI_OFFLINE"] = "1"
        env[PI_ALLOWED_READ_ROOTS_ENV] = os.pathsep.join(
            str(path) for path in pi_allowed_read_roots(self.workspace)
        )
        env[PI_PYTHON_BINARY_ENV] = pi_python_binary()
        env[PI_MEMORY_BRIDGE_PATH_ENV] = str(pi_memory_bridge_path())
        env[PI_EXA_BRIDGE_PATH_ENV] = str(pi_exa_bridge_path())
        env[PI_EXA_MCP_URL_ENV] = (
            os.environ.get(PI_EXA_MCP_URL_ENV, "").strip()
            or DEFAULT_PI_EXA_MCP_URL
        )
        env[PI_XIAOQING_BRIDGE_PATH_ENV] = str(pi_xiaoqing_bridge_path())
        env[PI_DINGTALK_IMAGE_BRIDGE_PATH_ENV] = str(
            pi_dingtalk_image_bridge_path()
        )
        env[PI_XIAOQING_MCP_URL_ENV] = (
            os.environ.get(PI_XIAOQING_MCP_URL_ENV, "").strip()
            or DEFAULT_PI_XIAOQING_MCP_URL
        )
        xiaoqing_access_token = os.environ.get(
            PI_XIAOQING_ACCESS_TOKEN_ENV,
            "",
        ).strip()
        if xiaoqing_access_token:
            env[PI_XIAOQING_ACCESS_TOKEN_ENV] = xiaoqing_access_token
        else:
            env.pop(PI_XIAOQING_ACCESS_TOKEN_ENV, None)
        env[PI_WORK_PROFILE_PATH_ENV] = str(work_profile_path().resolve())
        return env.copy()

    def build_command(
        self,
        prompt: str,
        session_id: str | None,
        image_paths: list[Path] | None = None,
        output_schema_path: Path | None = None,
        use_output_schema: bool = True,
        ignore_user_config: bool = False,
        approval_policy: str = "untrusted",
        developer_instructions: str | None = None,
        use_approval_bypass: bool = True,
        preserve_native_model_config: bool = False,
        profile_distillation: bool = False,
        allow_memory_writes: bool = True,
        thinking_level: str | None = None,
        tool_names: tuple[str, ...] | None = None,
    ) -> list[str]:
        del prompt
        del output_schema_path
        del use_output_schema
        del ignore_user_config
        del use_approval_bypass
        del preserve_native_model_config
        if approval_policy not in {"untrusted", "never"}:
            raise ValueError("unsupported approval policy")
        ensure_pi_runtime_config()
        model_selection = selected_pi_model_selection()
        extension_path = pi_extension_path()
        if not extension_path.is_file():
            raise ValueError(f"Pi reviewed extension does not exist: {extension_path}")
        command = [
            self.node_binary,
            str(self.pi_cli_path),
            "--mode",
            "json",
            "--offline",
            "--session-dir",
            str(pi_session_dir()),
            "--provider",
            model_selection.provider,
            "--model",
            model_selection.model,
            "--thinking",
            validate_pi_thinking_level(thinking_level)
            if thinking_level is not None
            else selected_pi_thinking_level(),
            "--approve",
            "--no-context-files",
            "--no-builtin-tools",
            "--no-extensions",
            "--extension",
            str(extension_path),
        ]
        if developer_instructions:
            command.extend(["--system-prompt", developer_instructions])
        if tool_names is not None and profile_distillation:
            raise ValueError("tool_names cannot be combined with profile distillation")
        if tool_names is not None:
            if any(not isinstance(tool, str) or not tool.strip() for tool in tool_names):
                raise ValueError("tool_names must contain non-empty strings")
            allowed_tools = tuple(tool.strip() for tool in tool_names)
        elif profile_distillation:
            if approval_policy != "never":
                raise ValueError("profile distillation requires never approval policy")
            allowed_tools = PROFILE_DISTILLATION_PI_TOOLS
        else:
            allowed_tools = READ_ONLY_PI_TOOLS
            if approval_policy == "untrusted":
                allowed_tools += EFFECTFUL_PI_TOOLS
                if not allow_memory_writes:
                    allowed_tools = tuple(
                        tool
                        for tool in allowed_tools
                        if tool not in MEMORY_WRITE_PI_TOOLS
                    )
        if allowed_tools:
            command.extend(["--tools", ",".join(allowed_tools)])
        else:
            command.append("--no-tools")
        if session_id:
            command.extend(["--session-id", session_id])
        for image_path in image_paths or []:
            command.append(f"@{image_path}")
        return command
