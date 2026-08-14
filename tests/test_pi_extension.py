from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.pi_runner import pi_cli_path, pi_extension_path, pi_node_binary


HARNESS = Path(__file__).parent / "fixtures" / "pi_extension_harness.mjs"


def _fake_dws(
    tmp_path: Path,
    *,
    tools: list[dict],
    stdout: str = "{}",
    stdout_by_command: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    config_path = tmp_path / "fake-dws.json"
    log_path = tmp_path / "fake-dws-log.jsonl"
    config_path.write_text(
        json.dumps(
            {
                "tools": tools,
                "stdout": stdout,
                "stdout_by_command": stdout_by_command or {},
                "log_path": str(log_path),
            }
        ),
        encoding="utf-8",
    )
    binary = binary_dir / "dws"
    binary.write_text(
        """#!/usr/bin/env python3
import base64
import json
import os
import sys

config = json.loads(open(os.environ["FAKE_DWS_CONFIG"], encoding="utf-8").read())
args = sys.argv[1:]
if args[:1] == ["schema"]:
    print(json.dumps({"products": [{"tools": config["tools"]}]}))
    raise SystemExit(0)
record = {
    "argv": ["dws", *args],
    "secrets": {
        key: os.environ[key]
        for key in (
            "CEO_PI_API_KEY",
            "OPENAI_API_KEY",
            "AUTHORIZATION",
            "PROVIDER_CLIENT_SECRET",
        )
        if key in os.environ
    },
}
with open(config["log_path"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
image_base64 = os.environ.get("FAKE_DWS_IMAGE_BASE64")
if image_base64:
    output_index = args.index("--output") + 1
    output_path = args[output_index]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as handle:
        handle.write(base64.b64decode(image_base64))
    sys.stdout.write(json.dumps({
        "localPath": output_path,
        "downloadUrl": "https://signed.example/private-image",
    }))
    raise SystemExit(0)
command = " ".join(args[:3])
if command in config.get("stdout_by_command", {}):
    sys.stdout.write(config["stdout_by_command"][command])
    raise SystemExit(0)
sys.stdout.write(config["stdout"])
""",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary_dir, log_path


def _fake_lark(
    tmp_path: Path,
    binary_dir: Path,
    *,
    tools: list[dict],
    shortcuts: dict[str, str],
    stdout: str,
) -> tuple[Path, Path]:
    config_path = tmp_path / "fake-lark.json"
    log_path = tmp_path / "fake-lark-log.jsonl"
    config_path.write_text(
        json.dumps(
            {
                "tools": tools,
                "shortcuts": shortcuts,
                "stdout": stdout,
                "log_path": str(log_path),
            }
        ),
        encoding="utf-8",
    )
    binary = binary_dir / "lark-cli"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

config = json.loads(open(os.environ["FAKE_LARK_CONFIG"], encoding="utf-8").read())
args = sys.argv[1:]
if args == ["schema"]:
    print(json.dumps(config["tools"]))
    raise SystemExit(0)
if args[-1:] == ["--help"]:
    command = " ".join(args[:-1])
    risk = config["shortcuts"].get(command)
    if not risk:
        raise SystemExit(2)
    print(f"Risk: {risk}")
    raise SystemExit(0)
record = {
    "argv": ["lark-cli", *args],
    "secrets": {
        key: os.environ[key]
        for key in (
            "CEO_PI_API_KEY",
            "OPENAI_API_KEY",
            "AUTHORIZATION",
            "PROVIDER_CLIENT_SECRET",
        )
        if key in os.environ
    },
}
with open(config["log_path"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
sys.stdout.write(config["stdout"])
""",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, log_path


def _fake_graphify(tmp_path: Path, binary_dir: Path) -> Path:
    log_path = tmp_path / "fake-graphify-log.json"
    binary = binary_dir / "graphify"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

record = {
    "argv": ["graphify", *sys.argv[1:]],
    "cwd": os.getcwd(),
    "secrets": {
        key: os.environ[key]
        for key in (
            "CEO_PI_API_KEY",
            "OPENAI_API_KEY",
            "AUTHORIZATION",
            "PROVIDER_CLIENT_SECRET",
        )
        if key in os.environ
    },
}
with open(os.environ["FAKE_GRAPHIFY_LOG"], "w", encoding="utf-8") as handle:
    json.dump(record, handle)
sys.stdout.write(os.environ.get("FAKE_GRAPHIFY_STDOUT", "{}"))
""",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return log_path


def _run_tool(
    tmp_path: Path,
    *,
    tool_name: str,
    params: dict,
    tools: list[dict] | None = None,
    stdout: str = "{}",
    extra_env: dict[str, str] | None = None,
    lark_tools: list[dict] | None = None,
    lark_shortcuts: dict[str, str] | None = None,
    lark_stdout: str = "{}",
    stdout_by_command: dict[str, str] | None = None,
) -> tuple[dict, Path]:
    binary_dir, log_path = _fake_dws(
        tmp_path,
        tools=tools or [],
        stdout=stdout,
        stdout_by_command=stdout_by_command,
    )
    lark_binary, _lark_log_path = _fake_lark(
        tmp_path,
        binary_dir,
        tools=lark_tools or [],
        shortcuts=lark_shortcuts or {},
        stdout=lark_stdout,
    )
    graphify_log_path = _fake_graphify(tmp_path, binary_dir)
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join((str(binary_dir), env.get("PATH", ""))),
            "FAKE_DWS_CONFIG": str(tmp_path / "fake-dws.json"),
            "CEO_PI_ALLOWED_READ_ROOTS": str(tmp_path),
            "CEO_FEISHU_CLI_BINARY": str(lark_binary),
            "FAKE_LARK_CONFIG": str(tmp_path / "fake-lark.json"),
            "FAKE_GRAPHIFY_LOG": str(graphify_log_path),
        }
    )
    env.update(extra_env or {})
    request = {
        "loaderPath": str(pi_cli_path().parent / "core" / "extensions" / "loader.js"),
        "extensionPath": str(pi_extension_path()),
        "cwd": str(tmp_path),
        "toolName": tool_name,
        "params": params,
    }
    completed = subprocess.run(
        [pi_node_binary(), str(HARNESS)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout), log_path


def _registered_tools(tmp_path: Path) -> list[str]:
    request = {
        "loaderPath": str(
            pi_cli_path().parent / "core" / "extensions" / "loader.js"
        ),
        "extensionPath": str(pi_extension_path()),
        "cwd": str(tmp_path),
        "listTools": True,
    }
    completed = subprocess.run(
        [pi_node_binary(), str(HARNESS)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    return payload["tools"]


def _metadata(
    cli_path: str,
    effect: str,
    *,
    confirmation: str = "not_required",
) -> dict:
    return {
        "cli_path": cli_path,
        "effect": effect,
        "availability": "available",
        "confirmation": confirmation,
    }


def _lark_metadata(name: str, risk: str, *, danger: bool = False) -> dict:
    return {
        "name": name,
        "_meta": {"risk": risk, "danger": danger},
    }


def test_extension_registers_all_reviewed_capabilities_together(tmp_path: Path):
    assert _registered_tools(tmp_path) == sorted(
        [
                "document_upload",
                "create_dingtalk_todo",
            "download_attachment",
            "download_dingtalk_image",
            "execute_reviewed_lark_read",
            "execute_reviewed_lark_write",
            "execute_reviewed_read",
            "execute_reviewed_write",
            "get_dashboard_stats",
            "get_interview_context",
            "graphify_read",
            "list_candidate_interviews",
            "memory_get",
            "memory_recall",
            "memory_write",
            "search_candidates",
            "timeline_get",
            "upload_interview_result",
            "user_get",
            "web_fetch_exa",
            "web_search_exa",
            "workspace_list",
            "workspace_read",
            "workspace_search",
            "write_work_profile",
        ]
    )


def test_graphify_read_executes_only_reviewed_operations_without_shell(
    tmp_path: Path,
):
    marker = tmp_path / "must-not-exist"
    query = f"dependency chain; touch {marker}"
    result, _ = _run_tool(
        tmp_path,
        tool_name="graphify_read",
        params={"operation": "query", "query": query},
        extra_env={
            "FAKE_GRAPHIFY_STDOUT": "reviewed graph result",
            "CEO_PI_API_KEY": "pi-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
        },
    )

    assert result["ok"] is True
    assert marker.exists() is False
    details = result["result"]["details"]
    assert details["cli"] == "graphify"
    assert details["effect"] == "read"
    assert details["operation"] == "graphify query"
    assert details["safeToConfirm"] is False
    record = json.loads(
        (tmp_path / "fake-graphify-log.json").read_text(encoding="utf-8")
    )
    assert record == {
        "argv": ["graphify", "query", query],
        "cwd": str(tmp_path),
        "secrets": {},
    }


def test_graphify_read_validates_path_shape_and_control_characters(tmp_path: Path):
    path_result, _ = _run_tool(
        tmp_path / "path",
        tool_name="graphify_read",
        params={"operation": "path", "source": "A", "target": "B"},
    )
    invalid_shape, _ = _run_tool(
        tmp_path / "shape",
        tool_name="graphify_read",
        params={"operation": "path", "query": "A to B"},
    )
    control_character, _ = _run_tool(
        tmp_path / "control",
        tool_name="graphify_read",
        params={"operation": "explain", "query": "A\nB"},
    )

    assert path_result["ok"] is True
    assert invalid_shape == {
        "ok": False,
        "error": "reviewed_graphify_arguments_invalid",
    }
    assert control_character == {
        "ok": False,
        "error": "reviewed_graphify_control_character",
    }


def _fake_xiaoqing_bridge(tmp_path: Path) -> tuple[Path, Path]:
    bridge = tmp_path / "fake-xiaoqing-bridge.py"
    log_path = tmp_path / "fake-xiaoqing-bridge-log.json"
    bridge.write_text(
        """import json
import os
import sys

request = json.load(sys.stdin)
arguments = request["arguments"]
dry_run = arguments.get("dry_run") is True
with open(os.environ["FAKE_XIAOQING_LOG"], "w", encoding="utf-8") as handle:
    json.dump({
        "request": request,
        "provider_secrets": {
            key: os.environ[key]
            for key in ("CEO_PI_API_KEY", "OPENAI_API_KEY", "AUTHORIZATION")
            if key in os.environ
        },
        "xiaoqing_token_present": bool(os.environ.get("CEO_PI_XIAOQING_ACCESS_TOKEN")),
    }, handle)
write = request["tool"] == "upload_interview_result" and not dry_run
response = {
    "ok": True,
    "tool": request["tool"],
    "result": {"status": "completed" if write else "ok"},
    "effect": "write" if write else "read",
    "confirmed": write,
    "receipt": {
        "result_record_id": "result-1",
        "processing_status": "completed",
    } if write else {},
}
json.dump(response, sys.stdout)
""",
        encoding="utf-8",
    )
    return bridge, log_path


def _fake_dingtalk_image_bridge(tmp_path: Path) -> tuple[Path, Path]:
    bridge = tmp_path / "fake-dingtalk-image-bridge.py"
    log_path = tmp_path / "fake-dingtalk-image-bridge-log.json"
    bridge.write_text(
        """import base64
import json
import os
import sys

request = json.load(sys.stdin)
with open(os.environ["FAKE_DINGTALK_IMAGE_LOG"], "w", encoding="utf-8") as handle:
    json.dump({
        "request": request,
        "provider_secrets": {
            key: os.environ[key]
            for key in ("CEO_PI_API_KEY", "OPENAI_API_KEY", "AUTHORIZATION")
            if key in os.environ
        },
        "robot_name": os.environ.get("CEO_DING_ROBOT_NAME", ""),
    }, handle)
image = b"\\x89PNG\\r\\n\\x1a\\nbridge-image"
json.dump({
    "ok": True,
    "tool": "download_dingtalk_image",
    "result": {
        "data": base64.b64encode(image).decode("ascii"),
        "mime_type": "image/png",
        "byte_length": len(image),
    },
    "effect": "read",
    "confirmed": False,
    "receipt": {},
}, sys.stdout)
""",
        encoding="utf-8",
    )
    return bridge, log_path


def test_reviewed_read_rejects_write_metadata(tmp_path: Path):
    result, _ = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_read",
        params={"argv": ["dws", "chat", "message", "send", "--conversation", "cid"]},
        tools=[_metadata("chat message send", "write")],
    )

    assert result == {"ok": False, "error": "reviewed_read_requires_read_effect"}


def test_reviewed_write_rejects_read_and_destructive_metadata(tmp_path: Path):
    read_result, _ = _run_tool(
        tmp_path / "read",
        tool_name="execute_reviewed_write",
        params={"argv": ["dws", "chat", "message", "list", "--conversation", "cid"]},
        tools=[_metadata("chat message list", "read")],
    )
    destructive_result, _ = _run_tool(
        tmp_path / "destructive",
        tool_name="execute_reviewed_write",
        params={"argv": ["dws", "doc", "delete", "--node-id", "node-1"]},
        tools=[_metadata("doc delete", "destructive", confirmation="user_required")],
    )

    assert read_result == {"ok": False, "error": "reviewed_write_requires_write_effect"}
    assert destructive_result["ok"] is False
    assert destructive_result["error"] in {
        "reviewed_user_confirmation_required",
        "reviewed_destructive_command_forbidden",
    }


@pytest.mark.parametrize("command", ["login", "logout", "reset", "install", "upgrade"])
def test_reviewed_extension_rejects_auth_and_install_segments(tmp_path: Path, command: str):
    result, _ = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_write",
        params={"argv": ["dws", "chat", command, "--id", "target"]},
        tools=[_metadata(f"chat {command}", "write")],
    )

    assert result == {"ok": False, "error": "reviewed_auth_or_install_command_forbidden"}


def test_reviewed_extension_rejects_user_required_write(tmp_path: Path):
    result, _ = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_write",
        params={"argv": ["dws", "aitable", "advperm", "disable", "--id", "base-1"]},
        tools=[
            _metadata(
                "aitable advperm disable",
                "write",
                confirmation="user_required",
            )
        ],
    )

    assert result == {"ok": False, "error": "reviewed_user_confirmation_required"}


def test_reviewed_extension_uses_argv_without_shell_and_strips_provider_secrets(
    tmp_path: Path,
):
    marker = tmp_path / "must-not-exist"
    argv = [
        "dws",
        "chat",
        "message",
        "send",
        "--conversation",
        "cid",
        ";",
        "touch",
        str(marker),
    ]
    result, log_path = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_write",
        params={"argv": argv},
        tools=[_metadata("chat message send", "write")],
        stdout=json.dumps({"messageId": "mid-1"}),
        extra_env={
            "CEO_PI_API_KEY": "pi-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
            "PROVIDER_CLIENT_SECRET": "provider-secret",
        },
    )

    assert result["ok"] is True
    assert marker.exists() is False
    details = result["result"]["details"]
    assert details["safeToConfirm"] is True
    assert details["targetIdentifiers"] == {"conversation": "cid"}
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert records == [{"argv": argv, "secrets": {}}]


def test_reviewed_dingtalk_reply_auto_mentions_original_group_trigger_sender(
    tmp_path: Path,
):
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    argv = [
        "dws",
        "chat",
        "message",
        "reply",
        "--conversation-id",
        "cid-1",
        "--ref-msg-id",
        "msg-1",
        "--ref-sender",
        "open-lily",
        "--text",
        "收到，我来处理。",
        "--format",
        "json",
        "--yes",
    ]
    result, log_path = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_write",
        params={"argv": argv},
        tools=[_metadata("chat message reply", "write")],
        stdout=json.dumps({"messageId": "reply-1"}),
        extra_env={
            "CEO_PI_REPLY_AT_OPEN_DINGTALK_ID": "open-lily",
            "CEO_PI_EXECUTION_RECEIPT_DIR": str(receipt_dir),
        },
    )

    assert result["ok"] is True
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["argv"][:14] == [
        "dws",
        "chat",
        "message",
        "reply",
        "--conversation-id",
        "cid-1",
        "--ref-msg-id",
        "msg-1",
        "--ref-sender",
        "open-lily",
        "--at-open-dingtalk-ids",
        "open-lily",
        "--text",
        "收到，我来处理。",
    ]
    assert records[0]["argv"][14] == "--uuid"
    assert len(records[0]["argv"][15]) == 36
    assert records[0]["argv"][16:] == ["--format", "json", "--yes"]
    # The receipt remains bound to the model-supplied argv; the adapter's
    # policy normalization is not a second, ambiguous operation.
    assert result["result"]["details"]["operationDigest"]
    reply_receipt = result["result"]["details"]["receipt"]
    assert reply_receipt == {
        "resultIdentifiers": {"messageId": "reply-1"},
        "processingStatus": "completed",
        "deliveryStatus": "sent",
        "idempotencyKey": reply_receipt["idempotencyKey"],
        "deliveredText": "收到，我来处理。",
    }
    assert len(reply_receipt["idempotencyKey"]) == 36
    assert len(list(receipt_dir.glob("*.json"))) == 1


def test_reviewed_dingtalk_reply_does_not_mention_single_chat_sender(
    tmp_path: Path,
):
    argv = [
        "dws",
        "chat",
        "message",
        "reply",
        "--conversation-id",
        "cid-1",
        "--ref-msg-id",
        "msg-1",
        "--ref-sender",
        "open-lily",
        "--text",
        "收到。",
        "--format",
        "json",
        "--yes",
    ]
    result, log_path = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_write",
        params={"argv": argv},
        tools=[_metadata("chat message reply", "write")],
        extra_env={
            "CEO_PI_REPLY_AT_OPEN_DINGTALK_ID": "open-lily",
            "CEO_PI_REPLY_SINGLE_CHAT": "1",
        },
    )

    assert result["ok"] is True
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert "--at-open-dingtalk-ids" not in record["argv"]


def test_dingtalk_todo_tool_resolves_people_creates_and_reads_back(
    tmp_path: Path,
):
    stdout_by_command = {
        "contact user get-self": json.dumps({"result": [{"orgEmployeeModel": {"userId": "self-1"}}]}),
        "todo task create": json.dumps({"result": {"taskId": "todo-1"}}),
        "todo task get": json.dumps({"result": {"taskId": "todo-1", "title": "测试"}}),
    }
    env = {
        "CEO_PI_TODO_TRIGGER_SENDER_NAME": "陈凯",
        "CEO_PI_TODO_TRIGGER_SENDER_USER_ID": "sender-1",
        "CEO_PI_TODO_TRIGGER_TEXT": "咱俩有个测试任务，周五之前完成，你记一个待办",
        "CEO_PI_TODO_TRIGGER_CREATE_TIME": "2026-08-13 10:00:00",
    }
    result, log_path = _run_tool(
        tmp_path,
        tool_name="create_dingtalk_todo",
        params={"title": "测试", "executor_names": ["咱俩"], "priority": 20},
        extra_env=env,
        stdout_by_command=stdout_by_command,
        tools=[
            _metadata("contact user get-self", "read"),
            _metadata("todo task create", "write"),
            _metadata("todo task get", "read"),
        ],
    )
    assert result["ok"] is True
    assert result["result"]["details"]["receipt"] == {
        "taskId": "todo-1",
        "readbackVerified": True,
    }
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [" ".join(record["argv"][1:4]) for record in records] == [
        "contact user get-self",
        "todo task create",
        "todo task get",
    ]
    create_argv = records[1]["argv"]
    assert create_argv[create_argv.index("--due") + 1] == "2026-08-14T18:00:00+08:00"


def test_dingtalk_todo_tool_resolves_absolute_chinese_due_date(tmp_path: Path):
    stdout_by_command = {
        "contact user get-self": json.dumps({"result": [{"userId": "self-1"}]}),
        "todo task create": json.dumps({"result": {"taskId": "todo-2"}}),
        "todo task get": json.dumps({"result": {"taskId": "todo-2"}}),
    }
    result, log_path = _run_tool(
        tmp_path,
        tool_name="create_dingtalk_todo",
        params={"title": "测试", "executor_names": ["self"]},
        extra_env={
            "CEO_PI_TODO_TRIGGER_TEXT": "请记一个待办，8月15日完成",
            "CEO_PI_TODO_TRIGGER_CREATE_TIME": "2026-08-13 10:00:00",
        },
        stdout_by_command=stdout_by_command,
        tools=[
            _metadata("contact user get-self", "read"),
            _metadata("todo task create", "write"),
            _metadata("todo task get", "read"),
        ],
    )
    assert result["ok"] is True
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    create_argv = records[1]["argv"]
    assert create_argv[create_argv.index("--due") + 1] == "2026-08-15T18:00:00+08:00"


def test_reviewed_dws_image_download_returns_pixels_and_deletes_temp_file(
    tmp_path: Path,
):
    png = b"\x89PNG\r\n\x1a\nreviewed-image"
    argv = [
        "dws",
        "chat",
        "message",
        "download-media",
        "--type",
        "mediaId",
        "--resource-id",
        "@image-1",
        "--message-id",
        "message-1",
        "--open-conversation-id",
        "conversation-1",
        "--output",
        "<local-path>",
        "--format",
        "json",
        "--yes",
    ]
    result, log_path = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_read",
        params={"argv": argv},
        tools=[_metadata("chat message download-media", "read")],
        extra_env={
            "FAKE_DWS_IMAGE_BASE64": base64.b64encode(png).decode(),
            "CEO_PI_API_KEY": "pi-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
        },
    )

    assert result["ok"] is True
    content = result["result"]["content"]
    assert content[0]["type"] == "text"
    assert "private-image" not in content[0]["text"]
    assert "localPath" not in content[0]["text"]
    assert content[1] == {
        "type": "image",
        "data": base64.b64encode(png).decode(),
        "mimeType": "image/png",
    }
    details = result["result"]["details"]
    assert details["effect"] == "read"
    assert details["imageAttached"] is True
    assert details["imageMimeType"] == "image/png"
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    output_path = Path(record["argv"][record["argv"].index("--output") + 1])
    assert output_path.name == "downloaded-image"
    assert str(output_path) != "<local-path>"
    assert output_path.exists() is False
    assert record["secrets"] == {}


def test_xiaoqing_extension_strips_provider_secrets_and_keeps_reviewed_token(
    tmp_path: Path,
):
    bridge, log_path = _fake_xiaoqing_bridge(tmp_path)
    result, _ = _run_tool(
        tmp_path,
        tool_name="search_candidates",
        params={"arguments": {"query": "engineering", "limit": 3}},
        extra_env={
            "CEO_PI_PYTHON_BINARY": sys.executable,
            "CEO_PI_XIAOQING_BRIDGE_PATH": str(bridge),
            "CEO_PI_XIAOQING_ACCESS_TOKEN": "xiaoqing-local-token",
            "FAKE_XIAOQING_LOG": str(log_path),
            "CEO_PI_API_KEY": "provider-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
        },
    )

    assert result["ok"] is True
    details = result["result"]["details"]
    assert details["bridge"] == "xiaoqing_interview"
    assert details["effect"] == "read"
    assert details["safeToConfirm"] is False
    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["request"] == {
        "tool": "search_candidates",
        "arguments": {"query": "engineering", "limit": 3},
    }
    assert record["provider_secrets"] == {}
    assert record["xiaoqing_token_present"] is True


def test_dingtalk_image_extension_returns_bridge_pixels_without_signed_url(
    tmp_path: Path,
):
    bridge, log_path = _fake_dingtalk_image_bridge(tmp_path)
    result, _ = _run_tool(
        tmp_path,
        tool_name="download_dingtalk_image",
        params={"download_code": "download-code-1"},
        extra_env={
            "CEO_PI_PYTHON_BINARY": sys.executable,
            "CEO_PI_DINGTALK_IMAGE_BRIDGE_PATH": str(bridge),
            "FAKE_DINGTALK_IMAGE_LOG": str(log_path),
            "CEO_DING_ROBOT_NAME": "Friday Robot",
            "CEO_PI_API_KEY": "provider-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
        },
    )

    assert result["ok"] is True
    content = result["result"]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    assert content[1]["mimeType"] == "image/png"
    assert result["result"]["details"]["bridge"] == "dingtalk_image"
    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record == {
        "request": {
            "tool": "download_dingtalk_image",
            "arguments": {"download_code": "download-code-1"},
        },
        "provider_secrets": {},
        "robot_name": "Friday Robot",
    }


def test_xiaoqing_extension_requires_completed_receipt_for_real_write(
    tmp_path: Path,
):
    bridge, log_path = _fake_xiaoqing_bridge(tmp_path)
    result, _ = _run_tool(
        tmp_path,
        tool_name="upload_interview_result",
        params={
            "arguments": {
                "candidate_id": "candidate-1",
                "summary": "reviewed",
                "dry_run": False,
            }
        },
        extra_env={
            "CEO_PI_PYTHON_BINARY": sys.executable,
            "CEO_PI_XIAOQING_BRIDGE_PATH": str(bridge),
            "CEO_PI_XIAOQING_ACCESS_TOKEN": "xiaoqing-local-token",
            "FAKE_XIAOQING_LOG": str(log_path),
        },
    )

    assert result["ok"] is True
    details = result["result"]["details"]
    assert details["effect"] == "write"
    assert details["safeToConfirm"] is True
    assert details["receipt"] == {
        "result_record_id": "result-1",
        "processing_status": "completed",
    }


def test_lark_extension_executes_official_read_and_shortcut_metadata(
    tmp_path: Path,
):
    raw_read, _ = _run_tool(
        tmp_path / "raw",
        tool_name="execute_reviewed_lark_read",
        params={
            "argv": [
                "lark-cli",
                "wiki",
                "spaces",
                "get",
                "--space-id",
                "space-1",
                "--json",
            ]
        },
        lark_tools=[_lark_metadata("wiki spaces get", "read")],
        lark_stdout=json.dumps({"space_id": "space-1", "name": "Team"}),
    )
    shortcut_read, _ = _run_tool(
        tmp_path / "shortcut",
        tool_name="execute_reviewed_lark_read",
        params={
            "argv": [
                "lark-cli",
                "docs",
                "+fetch",
                "--doc",
                "https://example.feishu.cn/docx/doc-1",
                "--json",
            ]
        },
        lark_shortcuts={"docs +fetch": "read"},
        lark_stdout=json.dumps({"document_id": "doc-1", "content": "reviewed"}),
    )

    assert raw_read["ok"] is True
    assert raw_read["result"]["details"]["operation"] == "wiki spaces get"
    assert raw_read["result"]["details"]["safeToConfirm"] is False
    assert shortcut_read["ok"] is True
    assert shortcut_read["result"]["details"]["operation"] == "docs +fetch"


def test_lark_extension_rejects_high_risk_and_auth_commands(tmp_path: Path):
    high_risk, _ = _run_tool(
        tmp_path / "high-risk",
        tool_name="execute_reviewed_lark_write",
        params={"argv": ["lark-cli", "drive", "files", "delete", "--yes"]},
        lark_tools=[
            _lark_metadata("drive files delete", "high-risk-write", danger=True)
        ],
    )
    auth, _ = _run_tool(
        tmp_path / "auth",
        tool_name="execute_reviewed_lark_write",
        params={"argv": ["lark-cli", "auth", "login"]},
        lark_tools=[_lark_metadata("auth login", "write")],
    )

    assert high_risk == {
        "ok": False,
        "error": "reviewed_destructive_command_forbidden",
    }
    assert auth == {
        "ok": False,
        "error": "reviewed_auth_or_install_command_forbidden",
    }


def test_lark_extension_write_requires_structured_resource_receipt(
    tmp_path: Path,
):
    result, _ = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_lark_write",
        params={
            "argv": [
                "lark-cli",
                "im",
                "messages",
                "create",
                "--receive-id",
                "chat-1",
                "--json",
            ]
        },
        lark_tools=[_lark_metadata("im messages create", "write")],
        lark_stdout=json.dumps({"message_id": "message-1"}),
        extra_env={
            "CEO_PI_API_KEY": "pi-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
        },
    )

    assert result["ok"] is True
    details = result["result"]["details"]
    assert details["cli"] == "lark-cli"
    assert details["effect"] == "write"
    assert details["safeToConfirm"] is True
    assert details["receipt"] == {
        "resultIdentifiers": {"message_id": "message-1"},
        "processingStatus": "completed",
    }


def test_workspace_read_rejects_outside_path_and_symlink_escape(tmp_path: Path):
    allowed = tmp_path / "allowed"
    forbidden = tmp_path / "forbidden"
    allowed.mkdir()
    forbidden.mkdir()
    secret = forbidden / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (allowed / "escape.txt").symlink_to(secret)
    common = {
        "tool_name": "workspace_read",
        "tools": [],
        "extra_env": {"CEO_PI_ALLOWED_READ_ROOTS": str(allowed)},
    }

    outside, _ = _run_tool(
        tmp_path / "outside-run",
        params={"path": str(secret)},
        **common,
    )
    symlink, _ = _run_tool(
        tmp_path / "symlink-run",
        params={"path": str(allowed / "escape.txt")},
        **common,
    )

    assert outside == {"ok": False, "error": "path_outside_reviewed_roots"}
    assert symlink == {"ok": False, "error": "path_outside_reviewed_roots"}


def test_work_profile_write_is_exact_atomic_and_receipted(tmp_path: Path):
    profile = tmp_path / "data" / "work-profile" / "work_profile.md"
    markdown = "# Reviewed profile\n\n- Prefer verified outcomes.\n"

    result, _ = _run_tool(
        tmp_path,
        tool_name="write_work_profile",
        params={"markdown": markdown},
        extra_env={
            "CEO_PI_WORK_PROFILE_PATH": str(profile),
            "CEO_PI_ALLOWED_READ_ROOTS": str(tmp_path),
        },
    )

    assert result["ok"] is True
    assert profile.read_text(encoding="utf-8") == markdown
    assert not list(profile.parent.glob("*.tmp"))
    details = result["result"]["details"]
    assert details["operation"] == "write_work_profile"
    assert details["targetIdentifiers"] == {"artifact": "work_profile"}
    assert details["safeToConfirm"] is True
    assert details["receipt"]["sha256"] == details["resultDigest"]


def test_work_profile_write_rejects_symlink_target(tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    profile = tmp_path / "data" / "work-profile" / "work_profile.md"
    profile.parent.mkdir(parents=True)
    profile.symlink_to(outside)

    result, _ = _run_tool(
        tmp_path,
        tool_name="write_work_profile",
        params={"markdown": "# must not write"},
        extra_env={
            "CEO_PI_WORK_PROFILE_PATH": str(profile),
            "CEO_PI_ALLOWED_READ_ROOTS": str(tmp_path),
        },
    )

    assert result == {"ok": False, "error": "work_profile_symlink_forbidden"}
    assert outside.read_text(encoding="utf-8") == "keep"


def test_reviewed_extension_enforces_output_limit(tmp_path: Path):
    result, _ = _run_tool(
        tmp_path,
        tool_name="execute_reviewed_read",
        params={"argv": ["dws", "chat", "message", "list", "--conversation", "cid"]},
        tools=[_metadata("chat message list", "read")],
        stdout="x" * (1024 * 1024 + 4096),
    )

    assert result["ok"] is False
    assert "maxBuffer" in result["error"] or "stdout maxBuffer length exceeded" in result["error"]


def test_memory_tool_uses_isolated_bridge_credentials_and_returns_receipt(tmp_path: Path):
    bridge = tmp_path / "fake-memory-bridge.py"
    bridge_log = tmp_path / "memory-bridge-log.json"
    bridge.write_text(
        """import json
import os
import sys

request = json.loads(sys.stdin.read())
record = {
    "request": request,
    "environment": {
        key: os.environ[key]
        for key in (
            "MEMORY_CONNECTOR_URL",
            "CONNECTOR_API_KEY",
            "MEMORY_CONNECTOR_AUTH_TYPE",
            "MEMORY_CONNECTOR_CONTENT_TYPE",
            "MEMORY_CONNECTOR_USER_ID",
            "CEO_PI_API_KEY",
            "OPENAI_API_KEY",
            "AUTHORIZATION",
        )
        if key in os.environ
    },
}
open(os.environ["FAKE_MEMORY_BRIDGE_LOG"], "w", encoding="utf-8").write(json.dumps(record))
sys.stdout.write(json.dumps({
    "ok": True,
    "tool": request["tool"],
    "result": {"episode_uuid": "episode-1", "processing_status": "completed"},
    "confirmed": True,
    "receipt": {"episode_uuid": "episode-1", "processing_status": "completed"},
}))
""",
        encoding="utf-8",
    )
    params = {
        "data": "Friday prefers concise release notes.",
        "type": "text",
        "created_at": "2026-08-08T00:00:00Z",
    }

    result, _ = _run_tool(
        tmp_path,
        tool_name="memory_write",
        params=params,
        extra_env={
            "CEO_PI_PYTHON_BINARY": sys.executable,
            "CEO_PI_MEMORY_BRIDGE_PATH": str(bridge),
            "FAKE_MEMORY_BRIDGE_LOG": str(bridge_log),
            "MEMORY_CONNECTOR_URL": "https://memory.example/mcp",
            "CONNECTOR_API_KEY": "memory-secret",
            "MEMORY_CONNECTOR_AUTH_TYPE": "api_key",
            "MEMORY_CONNECTOR_CONTENT_TYPE": "application/json",
            "MEMORY_CONNECTOR_USER_ID": "must-not-pass",
            "CEO_PI_API_KEY": "provider-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
        },
    )

    assert result["ok"] is True
    details = result["result"]["details"]
    assert details["bridge"] == "memory_connector"
    assert details["safeToConfirm"] is True
    assert details["receipt"] == {
        "episode_uuid": "episode-1",
        "processing_status": "completed",
    }
    record = json.loads(bridge_log.read_text(encoding="utf-8"))
    assert record["request"] == {"tool": "memory_write", "arguments": params}
    assert record["environment"] == {
        "MEMORY_CONNECTOR_URL": "https://memory.example/mcp",
        "CONNECTOR_API_KEY": "memory-secret",
        "MEMORY_CONNECTOR_AUTH_TYPE": "api_key",
        "MEMORY_CONNECTOR_CONTENT_TYPE": "application/json",
    }


def test_exa_tool_uses_reviewed_bridge_without_provider_credentials(tmp_path: Path):
    bridge = tmp_path / "fake-exa-bridge.py"
    bridge_log = tmp_path / "exa-bridge-log.json"
    bridge.write_text(
        """import json
import os
import sys

request = json.loads(sys.stdin.read())
record = {
    "request": request,
    "environment": {
        key: os.environ[key]
        for key in (
            "CEO_PI_EXA_MCP_URL",
            "CEO_PI_API_KEY",
            "OPENAI_API_KEY",
            "AUTHORIZATION",
            "CONNECTOR_API_KEY",
        )
        if key in os.environ
    },
}
open(os.environ["FAKE_EXA_BRIDGE_LOG"], "w", encoding="utf-8").write(json.dumps(record))
sys.stdout.write(json.dumps({
    "ok": True,
    "tool": request["tool"],
    "result": {"results": [{"url": "https://example.com"}]},
    "confirmed": False,
    "receipt": {},
}))
""",
        encoding="utf-8",
    )
    params = {"query": "Pi Agent release", "numResults": 2}

    result, _ = _run_tool(
        tmp_path,
        tool_name="web_search_exa",
        params=params,
        extra_env={
            "CEO_PI_PYTHON_BINARY": sys.executable,
            "CEO_PI_EXA_BRIDGE_PATH": str(bridge),
            "CEO_PI_EXA_MCP_URL": "https://mcp.exa.ai/mcp",
            "FAKE_EXA_BRIDGE_LOG": str(bridge_log),
            "CEO_PI_API_KEY": "provider-secret",
            "OPENAI_API_KEY": "provider-secret",
            "AUTHORIZATION": "Bearer provider-secret",
            "CONNECTOR_API_KEY": "memory-secret",
        },
    )

    assert result["ok"] is True
    details = result["result"]["details"]
    assert details["bridge"] == "exa"
    assert details["effect"] == "read"
    assert details["safeToConfirm"] is False
    assert details["receipt"] == {}
    record = json.loads(bridge_log.read_text(encoding="utf-8"))
    assert record["request"] == {"tool": "web_search_exa", "arguments": params}
    assert record["environment"] == {
        "CEO_PI_EXA_MCP_URL": "https://mcp.exa.ai/mcp"
    }
