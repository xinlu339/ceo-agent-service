import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import httpx

from app.memory_connector_config import (
    MEMORY_CONNECTOR_ENV_FILE,
    MEMORY_CONNECTOR_ENV_KEYS,
    memory_connector_env_from_config,
    parse_export_env_file,
)
from app.notification import send_macos_notification
from app.pi_capabilities import PiCapabilityReport, probe_pi_capabilities
from app.pi_runner import pi_memory_connector_env
from app.store import AutoReplyStore

MCP_DOCTOR_STATE_FILENAME = "mcp-doctor-state.json"
MCP_DOCTOR_ERROR_KIND = "mcp_doctor"
AUTHORIZATION_STATES = {"needs_login", "token_expired"}


@dataclass(frozen=True)
class McpStatus:
    name: str
    state: str
    ready: bool
    reason: str
    authorization_required: bool = False
    recover_command: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class McpDoctorState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def should_notify(self, status: McpStatus) -> bool:
        if status.ready or status.state not in AUTHORIZATION_STATES:
            return False
        payload = self._read()
        return self._notification_key(status) not in payload.get("notifications", {})

    def mark_notified(self, status: McpStatus, *, now: datetime | None = None) -> None:
        if status.ready:
            return
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        payload = self._read()
        notifications = payload.setdefault("notifications", {})
        notifications[self._notification_key(status)] = {
            "server": status.name,
            "state": status.state,
            "reason": status.reason,
            "notified_at": timestamp,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"notifications": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"notifications": {}}
        return payload if isinstance(payload, dict) else {"notifications": {}}

    @staticmethod
    def _notification_key(status: McpStatus) -> str:
        return f"{status.name}:{status.state}:{status.reason}"


def mcp_doctor_state_path(db_path: Path) -> Path:
    return db_path.expanduser().parent / MCP_DOCTOR_STATE_FILENAME


def check_mcp_statuses(
    *,
    memory_config_path: Path | None = None,
    verify_live: bool = False,
    memory_reachability_checker: Callable[[str], None] | None = None,
) -> list[McpStatus]:
    memory_env = _memory_environment(memory_config_path)
    report = probe_pi_capabilities(memory_env=memory_env)
    return [
        _memory_connector_status(
            report=report,
            verify_live=verify_live,
            memory_reachability_checker=memory_reachability_checker,
        ),
        _capability_status(
            report,
            "dws_reviewed_tools",
            name="dws_reviewed_tools",
        ),
        _capability_status(report, "graphify", name="graphify"),
        _capability_status(
            report,
            "xiaoqing_interview",
            name="xiaoqing_interview",
        ),
        _capability_status(report, "exa", name="exa"),
        _capability_status(report, "lark", name="lark"),
        _capability_status(report, "nvwa", name="nvwa"),
    ]


def record_and_notify_mcp_doctor(
    *,
    db_path: Path,
    statuses: Iterable[McpStatus],
    notify: bool = True,
    store_factory: Callable[[Path], AutoReplyStore] = AutoReplyStore,
    notification_sender: Callable[[str, str], None] | None = None,
) -> None:
    state = McpDoctorState(mcp_doctor_state_path(db_path))
    store = store_factory(db_path)
    sender = notification_sender or (
        lambda title, message: send_macos_notification(title=title, message=message)
    )
    for status in statuses:
        if status.ready:
            continue
        if state.should_notify(status):
            store.record_error(
                None,
                None,
                MCP_DOCTOR_ERROR_KIND,
                _error_detail(status),
            )
            if notify:
                sender(
                    f"CEO Pi capability needs authorization: {status.name}",
                    _notification_message(status),
                )
            state.mark_notified(status)


def mcp_doctor_report(
    *,
    db_path: Path,
    memory_config_path: Path | None = None,
    verify_live: bool = False,
    notify: bool = False,
) -> dict[str, object]:
    statuses = check_mcp_statuses(
        memory_config_path=memory_config_path,
        verify_live=verify_live,
    )
    if notify:
        record_and_notify_mcp_doctor(
            db_path=db_path,
            statuses=statuses,
            notify=True,
        )
    return {
        "ok": all(
            status.ready
            for status in statuses
            if status.state != "unsupported"
        ),
        "statuses": [status.as_dict() for status in statuses],
    }


def _memory_connector_status(
    *,
    report: PiCapabilityReport,
    verify_live: bool,
    memory_reachability_checker: Callable[[str], None] | None,
) -> McpStatus:
    bridge = report.get("memory_bridge")
    url = report.get("memory_url")
    api_key = report.get("memory_api_key")
    if not bridge.ready:
        return McpStatus(
            name="memory_connector",
            state="tool_not_found",
            ready=False,
            reason="reviewed Pi Memory Connector bridge is missing",
        )
    if not url.ready:
        return McpStatus(
            name="memory_connector",
            state=url.state,
            ready=False,
            reason="Memory Connector URL is missing or invalid for the reviewed Pi bridge",
            recover_command="ceo-agent setup-memory-connector --memory-url <memory-mcp-url>",
        )
    if not api_key.ready:
        return McpStatus(
            name="memory_connector",
            state="needs_login",
            ready=False,
            reason=(
                "Memory Connector API key is missing or expired; configure it locally "
                "and do not paste it into chat"
            ),
            authorization_required=True,
        )

    if verify_live:
        try:
            if memory_reachability_checker is not None:
                memory_reachability_checker(url.detail)
            else:
                _check_http_reachable(url.detail)
        except Exception as exc:
            return McpStatus(
                name="memory_connector",
                state=_network_or_tool_state(str(exc)),
                ready=False,
                reason=str(exc),
                recover_command="ceo-agent doctor-mcp --verify-live",
            )

    return McpStatus(
        name="memory_connector",
        state="ready",
        ready=True,
        reason="reviewed Pi Memory Connector bridge is configured",
    )


def _capability_status(
    report: PiCapabilityReport,
    key: str,
    *,
    name: str,
) -> McpStatus:
    capability = report.get(key)
    return McpStatus(
        name=name,
        state=capability.state,
        ready=capability.ready,
        reason=capability.detail,
        authorization_required=capability.state
        in {"needs_login", "missing_auth", "token_expired"},
    )


def _memory_environment(config_path: Path | None) -> dict[str, str]:
    if config_path is None:
        return pi_memory_connector_env()
    config_path = config_path.expanduser()
    file_env = parse_export_env_file(config_path.parent / MEMORY_CONNECTOR_ENV_FILE)
    whitelisted_file_env = {
        key: value for key, value in file_env.items() if key in MEMORY_CONNECTOR_ENV_KEYS
    }
    configured = memory_connector_env_from_config(config_path)
    process_env = {
        key: os.environ[key]
        for key in MEMORY_CONNECTOR_ENV_KEYS
        if os.environ.get(key)
    }
    merged = {
        **pi_memory_connector_env(),
        **configured,
        **whitelisted_file_env,
        **process_env,
    }
    merged.pop("MEMORY_CONNECTOR_USER_ID", None)
    return merged


def _network_or_tool_state(message: str) -> str:
    lowered = message.casefold()
    if any(
        marker in lowered
        for marker in (
            "network",
            "connection",
            "timeout",
            "temporary failure",
            "failed to resolve",
            "nodename nor servname",
        )
    ):
        return "network_blocked"
    if "authorization" in lowered or "unauthorized" in lowered:
        return "needs_login"
    return "tool_not_found"


def _check_http_reachable(url: str) -> None:
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        client.get(url)


def _error_detail(status: McpStatus) -> str:
    payload = status.as_dict()
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _notification_message(status: McpStatus) -> str:
    command = f" Run: {status.recover_command}." if status.recover_command else ""
    return (
        f"{status.name} is {status.state}: {status.reason}. "
        f"Related tasks are blocked until this is fixed.{command}"
    )[:240]
