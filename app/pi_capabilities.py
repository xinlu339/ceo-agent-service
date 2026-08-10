from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from app.bounded_process import ProcessOutputLimitError, run_bounded_process
from app.config import repo_root
from app.nvwa_review import nvwa_skill_path
from app.pi_runner import (
    DEFAULT_PI_EXA_MCP_URL,
    DEFAULT_PI_XIAOQING_MCP_URL,
    DEFAULT_PI_API,
    DEFAULT_PI_MODEL,
    DEFAULT_PI_PROVIDER,
    MINIMUM_PI_NODE_VERSION,
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
    PI_MODEL_SOURCE_ENV,
    PI_NODE_BINARY_ENV,
    PI_PROVIDER_ENV,
    pi_cli_path,
    pi_extension_path,
    pi_exa_bridge_path,
    pi_memory_bridge_path,
    pi_memory_connector_env,
    pi_models_config_for_values,
    pi_node_binary,
    pi_node_version,
    pi_xiaoqing_bridge_path,
    validate_pi_api,
    validate_pi_base_url,
    validate_pi_model,
    validate_pi_model_source,
    validate_pi_provider,
)


_SECRET_ENV_PATTERN = re.compile(
    r"(?:API[_-]?KEY|AUTHORIZATION|BEARER|CLIENT[_-]?SECRET|PRIVATE[_-]?KEY)",
    re.IGNORECASE,
)
_LARK_BINARY_ENV = "CEO_FEISHU_CLI_BINARY"
_EXTENSION_PROBE_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const [loaderPath, extensionPath, cwd] = process.argv.slice(1);
const loader = await import(pathToFileURL(loaderPath).href);
const loaded = await loader.loadExtensions([extensionPath], cwd);
if (loaded.errors.length > 0 || loaded.extensions.length !== 1) {
  process.stderr.write(JSON.stringify(loaded.errors));
  process.exit(1);
}
"""
_MODEL_RESOLUTION_PROBE_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const [runtimePath, resolverPath, modelsPath, authPath, modelsStorePath, provider, model, expectedApi, expectedBaseUrl] = process.argv.slice(1);
const { ModelRuntime } = await import(pathToFileURL(runtimePath).href);
const { resolveCliModel } = await import(pathToFileURL(resolverPath).href);
const runtime = await ModelRuntime.create({
  modelsPath,
  authPath,
  modelsStorePath,
  refreshOnCreate: false,
  allowModelNetwork: false,
});
const loadError = runtime.getError();
if (loadError) {
  process.stderr.write(loadError);
  process.exit(1);
}
const resolved = resolveCliModel({
  cliProvider: provider,
  cliModel: model,
  modelRuntime: runtime,
});
if (resolved.error || !resolved.model) {
  process.stderr.write(resolved.error || "model resolution failed");
  process.exit(1);
}
if (resolved.model.provider.toLowerCase() !== provider.toLowerCase() || resolved.model.id !== model) {
  process.stderr.write(`Requested ${provider}/${model} resolved to ${resolved.model.provider}/${resolved.model.id}`);
  process.exit(1);
}
if (resolved.model.api !== expectedApi) {
  process.stderr.write(`Requested API ${expectedApi} resolved to ${resolved.model.api}`);
  process.exit(1);
}
const actualBaseUrl = String(resolved.model.baseUrl || "").replace(/\/+$/u, "");
if (expectedBaseUrl && actualBaseUrl !== expectedBaseUrl) {
  process.stderr.write("Requested Base URL did not resolve to the configured endpoint");
  process.exit(1);
}
process.stdout.write(JSON.stringify({
  provider: resolved.model.provider,
  model: resolved.model.id,
  api: resolved.model.api,
  baseUrl: actualBaseUrl,
}));
"""

REQUESTED_PI_INTEGRATION_KEYS = (
    "dws_reviewed_tools",
    "memory_tools",
    "xiaoqing_interview",
    "exa",
    "lark",
    "nvwa",
)


@dataclass(frozen=True)
class PiCapability:
    key: str
    label: str
    state: str
    ready: bool
    detail: str
    required: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PiCapabilityReport:
    capabilities: tuple[PiCapability, ...]

    @property
    def runtime_ready(self) -> bool:
        return all(item.ready for item in self.capabilities if item.required)

    @property
    def integration_capabilities(self) -> tuple[PiCapability, ...]:
        requested = frozenset(REQUESTED_PI_INTEGRATION_KEYS)
        return tuple(item for item in self.capabilities if item.key in requested)

    @property
    def integrations_ready(self) -> bool:
        integrations = self.integration_capabilities
        return bool(integrations) and all(item.ready for item in integrations)

    @property
    def full_stack_ready(self) -> bool:
        return self.runtime_ready and self.integrations_ready

    def get(self, key: str) -> PiCapability:
        for item in self.capabilities:
            if item.key == key:
                return item
        raise KeyError(key)

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_ready": self.runtime_ready,
            "integrations_ready": self.integrations_ready,
            "full_stack_ready": self.full_stack_ready,
            "capabilities": [item.as_dict() for item in self.capabilities],
        }


def probe_pi_capabilities(
    *,
    env_values: Mapping[str, str] | None = None,
    root: Path | None = None,
    memory_env: Mapping[str, str] | None = None,
) -> PiCapabilityReport:
    root = (root or repo_root()).resolve()
    node_binary = _configured_value(
        env_values,
        PI_NODE_BINARY_ENV,
        fallback=pi_node_binary,
    )
    node_version = pi_node_version(node_binary)
    node_ready = node_version is not None and node_version >= MINIMUM_PI_NODE_VERSION
    node_version_text = (
        ".".join(str(part) for part in node_version)
        if node_version is not None
        else "unavailable"
    )

    cli_path = _configured_path(
        env_values,
        PI_CLI_PATH_ENV,
        fallback=pi_cli_path,
        root=root,
    )
    cli_ready = cli_path.is_file()
    extension_path = _configured_path(
        env_values,
        PI_EXTENSION_PATH_ENV,
        fallback=pi_extension_path,
        root=root,
    )
    extension_ready, extension_detail = _reviewed_extension_status(
        node_binary=node_binary,
        cli_path=cli_path,
        extension_path=extension_path,
        root=root,
        prerequisites_ready=node_ready and cli_ready,
    )

    provider_raw = _configured_raw(
        env_values,
        PI_PROVIDER_ENV,
        DEFAULT_PI_PROVIDER,
    )
    model_raw = _configured_raw(env_values, PI_MODEL_ENV, DEFAULT_PI_MODEL)
    model_source_raw = _configured_raw(
        env_values,
        PI_MODEL_SOURCE_ENV,
        "",
    ).strip()
    api_raw = _configured_raw(env_values, PI_API_ENV, DEFAULT_PI_API)
    base_url_raw = _configured_raw(env_values, PI_BASE_URL_ENV, "")
    try:
        provider = validate_pi_provider(provider_raw)
        model = validate_pi_model(model_raw)
        model_source = (
            validate_pi_model_source(model_source_raw)
            if model_source_raw
            else None
        )
        api = validate_pi_api(api_raw)
        base_url = validate_pi_base_url(base_url_raw)
    except ValueError as exc:
        provider_ready = False
        provider_detail = str(exc)
    else:
        provider_ready, resolution_detail = probe_pi_model_resolution(
            node_binary=node_binary,
            cli_path=cli_path,
            provider=provider,
            model=model,
            model_source=model_source,
            api=api,
            base_url=base_url,
            prerequisites_ready=node_ready and cli_ready,
        )
        endpoint = base_url or "provider default endpoint"
        provider_detail = (
            f"{provider} · {model} · {api} · {endpoint}"
            if provider_ready
            else resolution_detail
        )
    api_key_ready = bool(_configured_raw(env_values, PI_API_KEY_ENV, "").strip())

    dws_ready, dws_detail = _reviewed_dws_status()
    lark_binary = _configured_raw(env_values, _LARK_BINARY_ENV, "lark-cli").strip()
    lark_ready, lark_detail = _reviewed_lark_status(lark_binary or "lark-cli")

    bridge_path = _configured_path(
        env_values,
        PI_MEMORY_BRIDGE_PATH_ENV,
        fallback=pi_memory_bridge_path,
        root=root,
    )
    bridge_ready = bridge_path.is_file()
    connector_env = dict(
        pi_memory_connector_env() if memory_env is None else memory_env
    )
    memory_url = str(connector_env.get("MEMORY_CONNECTOR_URL") or "").strip()
    memory_key_ready = bool(
        str(connector_env.get("CONNECTOR_API_KEY") or "").strip()
    )
    memory_url_ready = _valid_memory_url(memory_url)
    memory_tools_ready = bridge_ready and memory_url_ready and memory_key_ready

    exa_bridge_path = _configured_path(
        env_values,
        PI_EXA_BRIDGE_PATH_ENV,
        fallback=pi_exa_bridge_path,
        root=root,
    )
    exa_url = _configured_raw(
        env_values,
        PI_EXA_MCP_URL_ENV,
        DEFAULT_PI_EXA_MCP_URL,
    ).strip()
    exa_url_ready = _valid_external_mcp_url(exa_url)
    exa_ready = exa_bridge_path.is_file() and exa_url_ready

    xiaoqing_bridge_path = _configured_path(
        env_values,
        PI_XIAOQING_BRIDGE_PATH_ENV,
        fallback=pi_xiaoqing_bridge_path,
        root=root,
    )
    xiaoqing_url = _configured_raw(
        env_values,
        PI_XIAOQING_MCP_URL_ENV,
        DEFAULT_PI_XIAOQING_MCP_URL,
    ).strip()
    xiaoqing_url_ready = _valid_external_mcp_url(xiaoqing_url)
    xiaoqing_token_ready = bool(
        _configured_raw(env_values, PI_XIAOQING_ACCESS_TOKEN_ENV, "").strip()
    )
    xiaoqing_ready = (
        xiaoqing_bridge_path.is_file()
        and xiaoqing_url_ready
        and xiaoqing_token_ready
    )

    nvwa_path = nvwa_skill_path()

    capabilities = (
        PiCapability(
            key="node",
            label="Node.js",
            state="ready" if node_ready else "blocked",
            ready=node_ready,
            detail=(
                f"{node_binary} · v{node_version_text} · minimum 22.19.0"
            ),
            required=True,
        ),
        PiCapability(
            key="pi_cli",
            label="Pi CLI",
            state="ready" if cli_ready else "blocked",
            ready=cli_ready,
            detail=str(cli_path),
            required=True,
        ),
        PiCapability(
            key="reviewed_extension",
            label="Reviewed Pi extension",
            state="ready" if extension_ready else "blocked",
            ready=extension_ready,
            detail=extension_detail,
            required=True,
        ),
        PiCapability(
            key="provider",
            label="Provider configuration",
            state="ready" if provider_ready else "blocked",
            ready=provider_ready,
            detail=provider_detail,
            required=True,
        ),
        PiCapability(
            key="provider_api_key",
            label="Provider API Key",
            state="ready" if api_key_ready else "missing_config",
            ready=api_key_ready,
            detail="Configured" if api_key_ready else "Not configured",
            required=True,
        ),
        PiCapability(
            key="dws_reviewed_tools",
            label="DWS reviewed tools",
            state="ready" if dws_ready else "blocked",
            ready=dws_ready,
            detail=dws_detail,
            required=True,
        ),
        PiCapability(
            key="memory_bridge",
            label="Memory reviewed bridge",
            state="ready" if bridge_ready else "blocked",
            ready=bridge_ready,
            detail=str(bridge_path),
        ),
        PiCapability(
            key="memory_url",
            label="Memory Connector URL",
            state=(
                "ready"
                if memory_url_ready
                else "invalid_config"
                if memory_url
                else "missing_config"
            ),
            ready=memory_url_ready,
            detail=(
                memory_url
                if memory_url_ready
                else "Invalid HTTP(S) URL"
                if memory_url
                else "Not configured"
            ),
        ),
        PiCapability(
            key="memory_api_key",
            label="Memory API Key",
            state="ready" if memory_key_ready else "missing_config",
            ready=memory_key_ready,
            detail="Configured" if memory_key_ready else "Not configured",
        ),
        PiCapability(
            key="memory_tools",
            label="Friday Memory tools",
            state="ready" if memory_tools_ready else "missing_config",
            ready=memory_tools_ready,
            detail=(
                "user_get, memory_recall, memory_get, timeline_get, "
                "memory_write, document_upload"
                if memory_tools_ready
                else "Bridge, URL, and a locally stored API key are all required"
            ),
        ),
        PiCapability(
            key="nvwa",
            label="Nvwa skill",
            state="ready" if nvwa_path is not None else "missing_config",
            ready=nvwa_path is not None,
            detail=str(nvwa_path) if nvwa_path is not None else "Not installed",
        ),
        PiCapability(
            key="xiaoqing_interview",
            label="Xiaoqing Interview",
            state="ready" if xiaoqing_ready else "missing_auth",
            ready=xiaoqing_ready,
            detail=(
                "5 reviewed reads + upload_interview_result · OAuth ready"
                if xiaoqing_ready
                else "Reviewed bridge is installed; local OAuth access token is required"
                if xiaoqing_bridge_path.is_file() and xiaoqing_url_ready
                else "Reviewed bridge and a safe HTTPS MCP URL are required"
            ),
        ),
        PiCapability(
            key="exa",
            label="Exa",
            state="ready" if exa_ready else "invalid_config",
            ready=exa_ready,
            detail=(
                f"web_search_exa, web_fetch_exa · {exa_url}"
                if exa_ready
                else "Reviewed bridge and a safe HTTPS MCP URL are required"
            ),
        ),
        PiCapability(
            key="lark",
            label="Lark CLI tools",
            state="ready" if lark_ready else "missing_cli",
            ready=lark_ready,
            detail=lark_detail,
        ),
    )
    return PiCapabilityReport(capabilities=capabilities)


def _configured_raw(
    env_values: Mapping[str, str] | None,
    key: str,
    default: str,
) -> str:
    if env_values is not None and key in env_values:
        return str(env_values[key])
    return os.environ.get(key, default)


def _valid_external_mcp_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and (
            parsed.scheme == "https"
            or _loopback_hostname(parsed.hostname or "")
        )
    )


def probe_pi_model_resolution(
    *,
    node_binary: str,
    cli_path: Path,
    provider: str,
    model: str,
    model_source: str | None = None,
    api: str,
    base_url: str,
    prerequisites_ready: bool = True,
) -> tuple[bool, str]:
    if not prerequisites_ready:
        return False, "Node.js and Pi CLI must be ready before model resolution"
    runtime_path = cli_path.parent / "core" / "model-runtime.js"
    resolver_path = cli_path.parent / "core" / "model-resolver.js"
    if not runtime_path.is_file() or not resolver_path.is_file():
        return False, "Pi model resolver modules are missing"
    try:
        sources = (
            (validate_pi_model_source(model_source),)
            if model_source
            else (("builtin", "custom") if base_url else ("builtin",))
        )
    except ValueError as exc:
        return False, str(exc)
    try:
        failures: list[str] = []
        for source in sources:
            ready, detail = _probe_pi_model_resolution_cached(
                node_binary,
                str(runtime_path.resolve()),
                str(resolver_path.resolve()),
                provider,
                model,
                source,
                api,
                base_url,
                runtime_path.stat().st_mtime_ns,
                resolver_path.stat().st_mtime_ns,
            )
            if ready:
                label = (
                    "Pi built-in model metadata"
                    if source == "builtin"
                    else "custom model metadata"
                )
                return True, f"{detail} · {label}"
            failures.append(detail)
        return False, failures[-1]
    except OSError as exc:
        return False, f"Pi model resolution failed: {type(exc).__name__}"


@lru_cache(maxsize=32)
def _probe_pi_model_resolution_cached(
    node_binary: str,
    runtime_path: str,
    resolver_path: str,
    provider: str,
    model: str,
    model_source: str,
    api: str,
    base_url: str,
    runtime_mtime_ns: int,
    resolver_mtime_ns: int,
) -> tuple[bool, str]:
    del runtime_mtime_ns, resolver_mtime_ns
    config = pi_models_config_for_values(
        provider=provider,
        model=model,
        model_source=model_source,
        api=api,
        base_url=base_url,
    )
    with tempfile.TemporaryDirectory(prefix="ceo-pi-model-probe-") as directory:
        root = Path(directory)
        models_path = root / "models.json"
        models_path.write_text(
            json.dumps(config, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        env = _safe_probe_environment()
        env["PI_OFFLINE"] = "1"
        try:
            completed = run_bounded_process(
                [
                    node_binary,
                    "--input-type=module",
                    "--eval",
                    _MODEL_RESOLUTION_PROBE_SCRIPT,
                    runtime_path,
                    resolver_path,
                    str(models_path),
                    str(root / "auth.json"),
                    str(root / "models-store.json"),
                    provider,
                    model,
                    api,
                    base_url,
                ],
                timeout=15,
                env=env,
                cwd=root,
            )
        except ProcessOutputLimitError:
            return False, "Pi model resolution output exceeded the limit"
        except subprocess.TimeoutExpired:
            return False, "Pi model resolution timed out"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Pi model resolution failed: {type(exc).__name__}"
    if completed.returncode != 0:
        return False, f"Pi model configuration is invalid: {_bounded_error(completed.stderr or completed.stdout)}"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False, "Pi model resolution returned invalid JSON"
    if not isinstance(payload, dict):
        return False, "Pi model resolution returned invalid output"
    return (
        True,
        f"Resolved offline: {provider}/{model} · {api} · "
        f"{base_url or 'provider default endpoint'}",
    )


def _configured_value(
    env_values: Mapping[str, str] | None,
    key: str,
    *,
    fallback,
) -> str:
    value = _configured_raw(env_values, key, "").strip()
    if value:
        return str(Path(os.path.expandvars(value)).expanduser())
    return str(fallback())


def _configured_path(
    env_values: Mapping[str, str] | None,
    key: str,
    *,
    fallback,
    root: Path,
) -> Path:
    value = _configured_raw(env_values, key, "").strip()
    path = (
        Path(os.path.expandvars(value)).expanduser()
        if value
        else Path(fallback())
    )
    return path if path.is_absolute() else (root / path).resolve()


def _reviewed_extension_status(
    *,
    node_binary: str,
    cli_path: Path,
    extension_path: Path,
    root: Path,
    prerequisites_ready: bool,
) -> tuple[bool, str]:
    if not extension_path.is_file():
        return False, f"Missing: {extension_path}"
    loader_path = cli_path.parent / "core" / "extensions" / "loader.js"
    if not loader_path.is_file():
        return False, f"Pi extension loader is missing: {loader_path}"
    if not prerequisites_ready:
        return False, "Node.js and Pi CLI must be ready before the extension can be loaded"
    try:
        key = (
            node_binary,
            str(loader_path.resolve()),
            str(extension_path.resolve()),
            str(root),
            loader_path.stat().st_mtime_ns,
            extension_path.stat().st_mtime_ns,
        )
        ready, error = _load_extension_cached(*key)
    except OSError as exc:
        return False, f"Extension probe failed: {type(exc).__name__}"
    if ready:
        return True, f"Loaded from {extension_path}"
    return False, f"Extension load failed: {error}"


@lru_cache(maxsize=16)
def _load_extension_cached(
    node_binary: str,
    loader_path: str,
    extension_path: str,
    root: str,
    loader_mtime_ns: int,
    extension_mtime_ns: int,
) -> tuple[bool, str]:
    del loader_mtime_ns, extension_mtime_ns
    try:
        completed = run_bounded_process(
            [
                node_binary,
                "--input-type=module",
                "--eval",
                _EXTENSION_PROBE_SCRIPT,
                loader_path,
                extension_path,
                root,
            ],
            timeout=15,
            env=_safe_probe_environment(),
            cwd=root,
        )
    except ProcessOutputLimitError:
        return False, "probe output exceeded the limit"
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    if completed.returncode == 0:
        return True, ""
    return False, _bounded_error(completed.stderr or completed.stdout)


def _reviewed_dws_status() -> tuple[bool, str]:
    binary = shutil.which("dws")
    if not binary:
        return False, "dws executable is not installed"
    try:
        mtime_ns = Path(binary).stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return _reviewed_dws_status_cached(binary, mtime_ns)


@lru_cache(maxsize=8)
def _reviewed_dws_status_cached(binary: str, mtime_ns: int) -> tuple[bool, str]:
    del mtime_ns
    try:
        completed = run_bounded_process(
            [binary, "schema", "--all", "--compact", "--format", "json"],
            timeout=30,
            env=_safe_probe_environment(),
        )
    except ProcessOutputLimitError:
        return False, "dws schema output exceeded the limit"
    except subprocess.TimeoutExpired:
        return False, "dws schema probe timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"dws schema probe failed: {type(exc).__name__}"
    if completed.returncode != 0:
        return False, f"dws schema failed: {_bounded_error(completed.stderr)}"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False, "dws schema returned invalid JSON"
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        return False, "dws schema has no products list"
    effects = [
        tool.get("effect")
        for product in products
        if isinstance(product, dict)
        for tool in product.get("tools", [])
        if isinstance(tool, dict)
    ]
    read_count = effects.count("read")
    write_count = effects.count("write")
    if read_count + write_count == 0:
        return False, "dws schema contains no reviewed read/write tools"
    return True, f"Installed schema: {read_count} read, {write_count} write tools"


def _reviewed_lark_status(configured_binary: str) -> tuple[bool, str]:
    binary = (
        configured_binary
        if Path(configured_binary).is_absolute()
        else shutil.which(configured_binary)
    )
    if not binary:
        return False, "lark-cli executable is not installed"
    if Path(binary).name != "lark-cli":
        return False, "configured Lark binary must be named lark-cli"
    try:
        mtime_ns = Path(binary).stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return _reviewed_lark_status_cached(str(binary), mtime_ns)


@lru_cache(maxsize=8)
def _reviewed_lark_status_cached(binary: str, mtime_ns: int) -> tuple[bool, str]:
    del mtime_ns
    try:
        completed = run_bounded_process(
            [binary, "schema"],
            timeout=60,
            env=_safe_probe_environment(),
        )
    except ProcessOutputLimitError:
        return False, "lark-cli schema output exceeded the limit"
    except subprocess.TimeoutExpired:
        return False, "lark-cli schema probe timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"lark-cli schema probe failed: {type(exc).__name__}"
    if completed.returncode != 0:
        return False, f"lark-cli schema failed: {_bounded_error(completed.stderr)}"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False, "lark-cli schema returned invalid JSON"
    if not isinstance(payload, list):
        return False, "lark-cli schema has no tools list"
    risks = [
        metadata.get("risk")
        for tool in payload
        if isinstance(tool, dict)
        for metadata in [tool.get("_meta")]
        if isinstance(metadata, dict)
    ]
    read_count = risks.count("read")
    write_count = risks.count("write")
    high_risk_count = risks.count("high-risk-write")
    if read_count + write_count == 0:
        return False, "lark-cli schema contains no reviewed read/write tools"
    return (
        True,
        f"Official schema: {read_count} read, {write_count} write; "
        f"{high_risk_count} high-risk writes blocked; login checked by Channel Gate",
    )


def _safe_probe_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("CEO_PI_") or _SECRET_ENV_PATTERN.search(key):
            env.pop(key, None)
    return env


def _valid_memory_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and (
            parsed.scheme == "https"
            or _loopback_hostname(parsed.hostname or "")
        )
    )


def _loopback_hostname(hostname: str) -> bool:
    normalized = hostname.casefold().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _unsupported_capability(key: str, label: str) -> PiCapability:
    return PiCapability(
        key=key,
        label=label,
        state="unsupported",
        ready=False,
        detail="Unsupported: no reviewed Pi tool is installed",
    )


def _bounded_error(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:500] or "unknown error"
