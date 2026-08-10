import hashlib
import json

from app.agent_result import EffectKind
from app.pi_tool_metadata import (
    reviewed_pi_command,
    structured_target_identifiers,
)


def test_reviewed_pi_command_captures_dws_write_identity_before_execution():
    argv = [
        "dws",
        "chat",
        "message",
        "send",
        "--group",
        "cid-1",
        "--text",
        "done",
        "--yes",
    ]

    command = reviewed_pi_command("execute_reviewed_write", {"argv": argv})

    assert command is not None
    assert command.cli == "dws"
    assert command.operation == "chat message send"
    assert command.effect is EffectKind.EFFECTFUL
    assert command.operation_digest == hashlib.sha256(
        json.dumps(argv, separators=(",", ":")).encode()
    ).hexdigest()
    assert command.target_identifiers == {"group": "cid-1"}


def test_reviewed_pi_command_captures_lark_read_identity():
    command = reviewed_pi_command(
        "execute_reviewed_lark_read",
        {
            "argv": [
                "lark-cli",
                "im",
                "messages",
                "list",
                "--receive-id",
                "chat-1",
            ]
        },
    )

    assert command is not None
    assert command.cli == "lark-cli"
    assert command.operation == "im messages list"
    assert command.effect is EffectKind.READ_ONLY
    assert command.target_identifiers == {"receive-id": "chat-1"}


def test_reviewed_pi_command_rejects_wrapper_cli_mismatch_and_dry_run():
    assert reviewed_pi_command(
        "execute_reviewed_write",
        {"argv": ["lark-cli", "im", "messages", "create"]},
    ) is None
    assert reviewed_pi_command(
        "execute_reviewed_write",
        {"argv": ["dws", "chat", "message", "send", "--dry-run"]},
    ) is None


def test_structured_target_identifiers_omits_credential_shaped_values():
    assert structured_target_identifiers(
        {
            "candidate_id": "candidate  1",
            "callback_url": "https://example.test/callback",
            "secret_id": "token=credential",
        }
    ) == {
        "candidate_id": "candidate  1",
        "callback_url": "https://example.test/callback",
    }
