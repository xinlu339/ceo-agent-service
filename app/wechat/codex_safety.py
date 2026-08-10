"""Fail-closed agent command and JSONL helpers for the WeChat Memory workflow."""
from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Iterator

from app.codex_runner import CODEX_BYPASS_APPROVALS_AND_SANDBOX
from app.pi_runner import READ_ONLY_PI_TOOLS

_TRANSPORT_OPTION = re.compile(
    r"^mcp_servers\.([A-Za-z0-9_-]+)\.(?:url|command)="
)
_TOOL_ITEM_TYPES = frozenset({
    "command_execution",
    "dynamic_tool_call",
    "function_call",
    "mcp_tool_call",
    "tool_call",
    "tool_search_call",
    "web_search",
    "web_search_call",
})
WECHAT_MEMORY_READ_TOOLS = (
    "memory_get",
    "memory_recall",
    "timeline_get",
    "user_get",
)
_PI_MEMORY_TOOL_NAMES = frozenset(
    {*WECHAT_MEMORY_READ_TOOLS, "memory_write", "document_upload"}
)


def _jsonl_payloads(raw: str) -> Iterator[dict]:
    for line in raw.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def completed_tool_events(raw: str) -> list[dict]:
    """Return completed tool events; lifecycle starts are never audit evidence."""
    events: list[dict] = []
    for payload in _jsonl_payloads(raw):
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in _TOOL_ITEM_TYPES or item_type.endswith("_tool_call"):
            events.append(item)
    events.extend(_completed_pi_memory_tool_calls(raw))
    return events


def completed_mcp_tool_calls(raw: str) -> list[dict]:
    completed_items: list[dict] = []
    for payload in _jsonl_payloads(raw):
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item")
        if isinstance(item, dict):
            completed_items.append(item)

    calls = [
        event
        for event in completed_items
        if event.get("type") == "mcp_tool_call"
    ]
    outputs = {
        str(event.get("call_id") or ""): event.get("output")
        for event in completed_items
        if event.get("type") == "function_call_output"
        and str(event.get("call_id") or "")
    }
    for event in completed_items:
        namespace = str(event.get("namespace") or "")
        if event.get("type") != "function_call" or not namespace.startswith("mcp__"):
            continue
        normalized = dict(event)
        normalized["type"] = "mcp_tool_call"
        normalized["tool"] = str(event.get("name") or "")
        normalized["result"] = outputs.get(str(event.get("call_id") or ""))
        calls.append(normalized)
    calls.extend(_completed_pi_memory_tool_calls(raw))
    return calls


def confirmed_pi_memory_write_receipt(
    result: object,
    *,
    arguments: dict[str, object],
) -> dict[str, str] | None:
    if not isinstance(result, dict):
        return None
    details = result.get("details")
    if not isinstance(details, dict):
        return None
    canonical = json.dumps(
        {"tool": "memory_write", "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        details.get("protocolVersion") != 1
        or details.get("bridge") != "memory_connector"
        or details.get("effect") != "write"
        or details.get("operation") != "memory_write"
        or details.get("operationDigest") != expected_digest
        or details.get("targetIdentifiers") != {}
        or details.get("exitCode") != 0
        or details.get("completed") is not True
        or details.get("safeToConfirm") is not True
    ):
        return None
    receipt = details.get("receipt")
    if not isinstance(receipt, dict):
        return None
    episode_uuid = receipt.get("episode_uuid")
    if (
        not isinstance(episode_uuid, str)
        or not episode_uuid.strip()
        or receipt.get("processing_status") != "completed"
    ):
        return None
    return {
        "episode_uuid": episode_uuid.strip(),
        "processing_status": "completed",
    }


def _completed_pi_memory_tool_calls(raw: str) -> list[dict]:
    starts: dict[str, dict] = {}
    completed: list[dict] = []
    for payload in _jsonl_payloads(raw):
        event_type = payload.get("type")
        call_id = str(payload.get("toolCallId") or "").strip()
        tool = str(payload.get("toolName") or "").strip()
        if event_type == "tool_execution_start" and call_id and tool in _PI_MEMORY_TOOL_NAMES:
            starts[call_id] = payload
            continue
        if event_type != "tool_execution_end" or not call_id:
            continue
        start = starts.pop(call_id, None)
        if not isinstance(start, dict):
            continue
        tool = str(start.get("toolName") or "").strip()
        if tool not in _PI_MEMORY_TOOL_NAMES:
            continue
        completed.append(
            {
                "type": "mcp_tool_call",
                "call_id": call_id,
                "server": "memory_connector",
                "tool": tool,
                "arguments": start.get("args"),
                "result": payload.get("result"),
                "isError": payload.get("isError") is True,
            }
        )
    return completed


def has_any_tool_event(raw: str) -> bool:
    """Detect any Codex or Pi tool lifecycle event, including attempted calls."""
    for payload in _jsonl_payloads(raw):
        if payload.get("type") in {"tool_execution_start", "tool_execution_end"}:
            return True
        if payload.get("type") == "message":
            message = payload.get("message")
            if isinstance(message, dict):
                if message.get("role") in {"toolResult", "bashExecution"}:
                    return True
                content = message.get("content")
                if isinstance(content, list) and any(
                    isinstance(block, dict) and block.get("type") == "toolCall"
                    for block in content
                ):
                    return True
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in _TOOL_ITEM_TYPES or item_type.endswith("_tool_call"):
            return True
    return False


def configured_transport_server_names(
    command: list[str], *, include_all_configured: bool = False
) -> tuple[str, ...]:
    """Find MCP transports relevant to the requested isolation boundary."""
    from app.codex_runner import _codex_config, _passthrough_mcp_server_names

    names: set[str] = set()
    explicitly_configured_names: set[str] = set()
    passthrough_names = frozenset(_passthrough_mcp_server_names())
    servers = _codex_config().get("mcp_servers") or {}
    if isinstance(servers, dict):
        for name, server in servers.items():
            if not isinstance(name, str) or not isinstance(server, dict):
                continue
            if any(
                isinstance(server.get(key), str) and server[key].strip()
                for key in ("url", "command")
            ):
                explicitly_configured_names.add(name)
                if include_all_configured or name in passthrough_names:
                    names.add(name)
    for index, value in enumerate(command[:-1]):
        if value != "-c" or index + 1 >= len(command):
            continue
        match = _TRANSPORT_OPTION.match(command[index + 1])
        if match:
            name = match.group(1)
            if (
                include_all_configured
                or name != "exa"
                or name in explicitly_configured_names
            ):
                names.add(name)
    return tuple(sorted(names))


def disable_configured_mcp_servers(
    command: list[str], *, except_names: frozenset[str] = frozenset(),
    include_all_configured: bool = False,
) -> None:
    if _is_pi_command(command):
        return
    for name in configured_transport_server_names(
        command, include_all_configured=include_all_configured
    ):
        if name not in except_names:
            _insert_command_options(
                command, ["-c", f"mcp_servers.{name}.enabled=false"]
            )


def make_read_only_without_tools(command: list[str]) -> None:
    """Constrain extraction to an agent invocation with no tools."""
    if _is_pi_command(command):
        _set_pi_tools(command, None)
        return
    while CODEX_BYPASS_APPROVALS_AND_SANDBOX in command:
        command.remove(CODEX_BYPASS_APPROVALS_AND_SANDBOX)
    _remove_config_options(
        command,
        prefixes=("approval_policy=", "approvals_reviewer=", "tools.enabled_tools="),
    )
    disable_configured_mcp_servers(command, include_all_configured=True)
    _insert_command_options(
        command,
        [
            "--sandbox", "read-only",
            "-c", 'approval_policy="never"',
            "-c", "tools.enabled_tools=[]",
            "-c", 'web_search="disabled"',
        ],
    )


def make_read_only_with_memory_tools(command: list[str]) -> None:
    """Allow only durable-memory reads while a WeChat reply is being decided."""
    if _is_pi_command(command):
        _set_pi_tools(command, WECHAT_MEMORY_READ_TOOLS)
        return
    while CODEX_BYPASS_APPROVALS_AND_SANDBOX in command:
        command.remove(CODEX_BYPASS_APPROVALS_AND_SANDBOX)
    _remove_config_options(
        command,
        prefixes=("approval_policy=", "approvals_reviewer=", "tools.enabled_tools="),
    )
    disable_configured_mcp_servers(
        command,
        except_names=frozenset({"memory_connector"}),
        include_all_configured=True,
    )
    enabled_tools = json.dumps(
        list(WECHAT_MEMORY_READ_TOOLS), ensure_ascii=True, separators=(",", ":")
    )
    _insert_command_options(
        command,
        [
            "-c",
            "features.plugins=false",
            "-c",
            "features.apps=false",
            "--sandbox",
            "read-only",
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            f"mcp_servers.memory_connector.enabled_tools={enabled_tools}",
        ],
    )


def make_read_only_with_reviewed_tools(
    command: list[str],
    *,
    reviewed_mcp_tools: dict[str, tuple[str, ...]],
    controlled_cli_command: str,
    controlled_cli_args: tuple[str, ...],
    controlled_cli_cwd: str,
) -> None:
    """Use a read-only sandbox and expose only explicitly reviewed MCP reads."""
    if _is_pi_command(command):
        _set_pi_tools(command, READ_ONLY_PI_TOOLS)
        return
    while CODEX_BYPASS_APPROVALS_AND_SANDBOX in command:
        command.remove(CODEX_BYPASS_APPROVALS_AND_SANDBOX)
    _remove_config_options(
        command,
        prefixes=("approval_policy=", "approvals_reviewer=", "tools.enabled_tools="),
    )
    configured = configured_transport_server_names(
        command,
        include_all_configured=True,
    )
    for name in configured:
        tools = reviewed_mcp_tools.get(name, ())
        if not tools:
            _insert_command_options(
                command,
                ["-c", f"mcp_servers.{name}.enabled=false"],
            )
            continue
        encoded = json.dumps(list(tools), ensure_ascii=True, separators=(",", ":"))
        _insert_command_options(
            command,
            ["-c", f"mcp_servers.{name}.enabled_tools={encoded}"],
        )
    _insert_command_options(
        command,
        [
            "-c",
            "features.plugins=false",
            "-c",
            "features.apps=false",
            "--sandbox",
            "read-only",
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            f"mcp_servers.reconciliation_cli.command={json.dumps(controlled_cli_command)}",
            "-c",
            "mcp_servers.reconciliation_cli.args="
            + json.dumps(list(controlled_cli_args), ensure_ascii=True),
            "-c",
            f"mcp_servers.reconciliation_cli.cwd={json.dumps(controlled_cli_cwd)}",
            "-c",
            'mcp_servers.reconciliation_cli.enabled_tools=["execute_reviewed_read","read_skill"]',
        ],
    )


def make_direct_agent_sandbox(
    command: list[str],
    *,
    reviewed_mcp_tools: dict[str, tuple[str, ...]],
    controlled_cli_command: str,
    controlled_cli_args: tuple[str, ...],
    controlled_cli_cwd: str,
) -> None:
    """Expose sandboxed local reads and only reviewed external capabilities."""
    if _is_pi_command(command):
        _set_pi_tools(command, READ_ONLY_PI_TOOLS)
        return
    while CODEX_BYPASS_APPROVALS_AND_SANDBOX in command:
        command.remove(CODEX_BYPASS_APPROVALS_AND_SANDBOX)
    _remove_command_options(command, names=("--sandbox",))
    _remove_config_options(
        command,
        prefixes=("approval_policy=", "tools.enabled_tools=", "web_search="),
    )
    configured = configured_transport_server_names(
        command,
        include_all_configured=True,
    )
    for name in configured:
        tools = reviewed_mcp_tools.get(name, ())
        if not tools:
            _insert_command_options(
                command,
                ["-c", f"mcp_servers.{name}.enabled=false"],
            )
            continue
        encoded = json.dumps(list(tools), ensure_ascii=True, separators=(",", ":"))
        _insert_command_options(
            command,
            ["-c", f"mcp_servers.{name}.enabled_tools={encoded}"],
        )
    _insert_command_options(
        command,
        [
            "-c",
            "features.plugins=false",
            "-c",
            "features.apps=false",
            "--sandbox",
            "read-only",
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            f"mcp_servers.reconciliation_cli.command={json.dumps(controlled_cli_command)}",
            "-c",
            "mcp_servers.reconciliation_cli.args="
            + json.dumps(list(controlled_cli_args), ensure_ascii=True),
            "-c",
            f"mcp_servers.reconciliation_cli.cwd={json.dumps(controlled_cli_cwd)}",
            "-c",
            "mcp_servers.reconciliation_cli.enabled_tools="
            '["execute_reviewed_read","execute_reviewed_write","read_skill"]',
        ],
    )


def _insert_command_options(command: list[str], options: list[str]) -> None:
    prompt_index = len(command) - 1
    if command[1:3] == ["exec", "resume"]:
        prompt_index -= 1
    command[prompt_index:prompt_index] = options


def _is_pi_command(command: list[str]) -> bool:
    return "--mode" in command and "json" in command


def _set_pi_tools(command: list[str], tools: tuple[str, ...] | None) -> None:
    index = 0
    while index < len(command):
        if command[index] == "--no-tools":
            del command[index]
            continue
        if command[index] == "--tools":
            del command[index : index + 2]
            continue
        index += 1
    insert_at = next(
        (index for index, value in enumerate(command) if value.startswith("@")),
        len(command),
    )
    options = ["--no-tools"] if tools is None else ["--tools", ",".join(tools)]
    command[insert_at:insert_at] = options


def _remove_config_options(command: list[str], *, prefixes: tuple[str, ...]) -> None:
    index = 0
    while index + 1 < len(command):
        if command[index] == "-c" and command[index + 1].startswith(prefixes):
            del command[index : index + 2]
            continue
        index += 1


def _remove_command_options(command: list[str], *, names: tuple[str, ...]) -> None:
    index = 0
    while index + 1 < len(command):
        if command[index] in names:
            del command[index : index + 2]
            continue
        index += 1
