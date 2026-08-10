import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from app.config import repo_root, work_profile_path
from app.dws_client import dws_noninteractive_environment
from app.pi_events import pi_stream_completion_issue


PI_NODE_BINARY_ENV = "CEO_PI_NODE_BINARY"
PI_CLI_PATH_ENV = "CEO_PI_CLI_PATH"
PI_PROVIDER_ENV = "CEO_PI_PROVIDER"
PI_MODEL_ENV = "CEO_PI_MODEL"
PI_API_ENV = "CEO_PI_API"
PI_BASE_URL_ENV = "CEO_PI_BASE_URL"
PI_API_KEY_ENV = "CEO_PI_API_KEY"
PI_THINKING_LEVEL_ENV = "CEO_PI_THINKING_LEVEL"
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
PI_WORK_PROFILE_PATH_ENV = "CEO_PI_WORK_PROFILE_PATH"

DEFAULT_PI_PROVIDER = "openai"
DEFAULT_PI_MODEL = "gpt-5.5"
DEFAULT_PI_API = "openai-responses"
DEFAULT_PI_THINKING_LEVEL = "medium"
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
READ_ONLY_PI_TOOLS = (
    "workspace_read",
    "workspace_search",
    "workspace_list",
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
PROFILE_DISTILLATION_PI_TOOLS = (
    "workspace_read",
    "workspace_search",
    "workspace_list",
    "write_work_profile",
)
MINIMUM_PI_NODE_VERSION = (22, 19, 0)
_PI_PROVIDER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_NODE_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


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
            Path.home() / ".codex" / "skills",
        ]
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def selected_pi_provider() -> str:
    value = os.environ.get(PI_PROVIDER_ENV, DEFAULT_PI_PROVIDER)
    return validate_pi_provider(value)


def validate_pi_provider(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_PROVIDER
    if not _PI_PROVIDER_PATTERN.fullmatch(value):
        raise ValueError("CEO_PI_PROVIDER contains unsupported characters")
    return value


def selected_pi_model() -> str:
    return validate_pi_model(os.environ.get(PI_MODEL_ENV, DEFAULT_PI_MODEL))


def validate_pi_model(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_MODEL
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("CEO_PI_MODEL must be one non-empty model id")
    return value


def selected_pi_api() -> str:
    return validate_pi_api(os.environ.get(PI_API_ENV, DEFAULT_PI_API))


def validate_pi_api(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_API
    if value not in SUPPORTED_PI_APIS:
        raise ValueError(f"unsupported Pi API protocol: {value}")
    return value


def selected_pi_thinking_level() -> str:
    return validate_pi_thinking_level(
        os.environ.get(PI_THINKING_LEVEL_ENV, DEFAULT_PI_THINKING_LEVEL)
    )


def validate_pi_thinking_level(raw_value: str) -> str:
    value = raw_value.strip() or DEFAULT_PI_THINKING_LEVEL
    if value not in SUPPORTED_PI_THINKING_LEVELS:
        raise ValueError(f"unsupported Pi thinking level: {value}")
    return value


def selected_pi_base_url() -> str:
    return validate_pi_base_url(os.environ.get(PI_BASE_URL_ENV, ""))


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


def pi_models_config() -> dict[str, object]:
    return pi_models_config_for_values(
        provider=selected_pi_provider(),
        model=selected_pi_model(),
        api=selected_pi_api(),
        base_url=selected_pi_base_url(),
    )


def pi_models_config_for_values(
    *,
    provider: str,
    model: str,
    api: str,
    base_url: str,
) -> dict[str, object]:
    provider = validate_pi_provider(provider)
    model = validate_pi_model(model)
    api = validate_pi_api(api)
    base_url = validate_pi_base_url(base_url)
    provider_config: dict[str, object] = {
        "apiKey": f"${PI_API_KEY_ENV}",
    }
    if base_url:
        provider_config.update(
            {
                "baseUrl": base_url,
                "api": api,
                "models": [
                    {
                        "id": model,
                        "name": model,
                    }
                ],
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
        base_env = os.environ.copy()
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
            selected_pi_provider(),
            "--model",
            selected_pi_model(),
            "--thinking",
            selected_pi_thinking_level(),
            "--approve",
            "--no-context-files",
            "--no-builtin-tools",
            "--no-extensions",
            "--extension",
            str(extension_path),
        ]
        if developer_instructions:
            command.extend(["--system-prompt", developer_instructions])
        if profile_distillation:
            if approval_policy != "never":
                raise ValueError("profile distillation requires never approval policy")
            allowed_tools = PROFILE_DISTILLATION_PI_TOOLS
        else:
            allowed_tools = READ_ONLY_PI_TOOLS
            if approval_policy == "untrusted":
                allowed_tools += EFFECTFUL_PI_TOOLS
        command.extend(["--tools", ",".join(allowed_tools)])
        if session_id:
            command.extend(["--session-id", session_id])
        for image_path in image_paths or []:
            command.append(f"@{image_path}")
        return command
