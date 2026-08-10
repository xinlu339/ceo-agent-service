from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.agent_result import EffectKind
from app.leak_check import contains_credential


MAX_REVIEWED_ARG_COUNT = 96
MAX_REVIEWED_ARG_BYTES = 64 * 1024

_REVIEWED_WRAPPERS: dict[str, tuple[str, EffectKind]] = {
    "execute_reviewed_read": ("dws", EffectKind.READ_ONLY),
    "execute_reviewed_write": ("dws", EffectKind.EFFECTFUL),
    "execute_reviewed_lark_read": ("lark-cli", EffectKind.READ_ONLY),
    "execute_reviewed_lark_write": ("lark-cli", EffectKind.EFFECTFUL),
}


@dataclass(frozen=True)
class PiReviewedCommand:
    cli: str
    operation: str
    effect: EffectKind
    operation_digest: str
    target_identifiers: dict[str, str]


def reviewed_pi_command(
    tool_name: str,
    arguments: object,
) -> PiReviewedCommand | None:
    """Describe one reviewed Pi CLI wrapper call without rediscovering CLI schema.

    The Pi Extension remains authoritative for whether the exact installed DWS or
    Lark command is allowed. This helper only captures conservative identity at
    ``tool_execution_start`` so an interrupted write is durable before execution.
    """

    wrapper = _REVIEWED_WRAPPERS.get(tool_name)
    argv = arguments.get("argv") if isinstance(arguments, dict) else None
    if wrapper is None or not _valid_argv(argv):
        return None
    assert isinstance(argv, list)
    cli, effect = wrapper
    if Path(argv[0]).name != cli or "--dry-run" in argv:
        return None
    command_tokens: list[str] = []
    for token in argv[1:]:
        if token.startswith("-"):
            break
        command_tokens.append(token)
    if not command_tokens:
        return None
    return PiReviewedCommand(
        cli=cli,
        operation=" ".join(command_tokens),
        effect=effect,
        operation_digest=reviewed_argv_digest(argv),
        target_identifiers=argv_target_identifiers(argv),
    )


def reviewed_argv_digest(argv: object) -> str:
    if not _valid_argv(argv):
        return ""
    serialized = json.dumps(
        argv,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def argv_target_identifiers(argv: list[str]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    index = 1
    while index < len(argv) and len(identifiers) < 32:
        token = argv[index]
        if not token.startswith("--"):
            index += 1
            continue
        flag, separator, inline_value = token[2:].partition("=")
        normalized = flag.replace("_", "-").casefold()
        is_target = normalized.endswith("-id") or normalized in {
            "id",
            "conversation",
            "group",
            "email",
            "node",
            "url",
        }
        if separator:
            value = inline_value
        elif index + 1 < len(argv) and not argv[index + 1].startswith("-"):
            value = argv[index + 1]
            index += 1
        else:
            value = ""
        if is_target and value and not contains_credential(value):
            identifiers[normalized] = value[:500]
        index += 1
    return identifiers


def structured_target_identifiers(value: object) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    stack: list[object] = [value]
    while stack and len(identifiers) < 32:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current[:64])
            continue
        if not isinstance(current, dict):
            continue
        for key, item in current.items():
            normalized = str(key).replace("_", "").replace("-", "").casefold()
            if isinstance(item, str) and (
                normalized == "id"
                or normalized.endswith("id")
                or normalized.endswith("url")
                or normalized == "uuid"
            ):
                if item and not contains_credential(item):
                    identifiers[str(key)] = item[:500]
            elif isinstance(item, dict | list):
                stack.append(item)
    return identifiers


def _valid_argv(value: object) -> bool:
    if (
        not isinstance(value, list)
        or not 2 <= len(value) <= MAX_REVIEWED_ARG_COUNT
        or not all(isinstance(item, str) for item in value)
    ):
        return False
    byte_count = 0
    for item in value:
        if any(character in item for character in ("\x00", "\n", "\r")):
            return False
        byte_count += len(item.encode("utf-8"))
    return byte_count <= MAX_REVIEWED_ARG_BYTES
