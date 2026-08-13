"""Deterministic routing helpers for DingTalk Todo creation requests."""

from __future__ import annotations

import re


# Keep this matcher deliberately focused on creation.  Query and mutation
# requests such as "查待办" or "标记待办完成" must continue through the normal
# agent path because they have different commands and confirmation semantics.
_TODO_CREATE_PATTERN = re.compile(
    r"(?:"
    r"记(?:一个|一下|条)?\s*(?:钉钉)?待办"
    r"|创建(?:一个|一条)?\s*(?:钉钉)?待办"
    r"|新增(?:一个|一条)?\s*(?:钉钉)?待办"
    r"|新建(?:一个|一条)?\s*(?:钉钉)?待办"
    r"|建(?:一个|一条)?\s*(?:钉钉)?待办"
    r"|(?:帮我)?记(?:一下|一个|一条)?任务"
    r"|任务提醒"
    r"|\bTODO\b"
    r")",
    re.IGNORECASE,
)
_TODO_QUERY_OR_MUTATION_PATTERN = re.compile(
    r"(?:查|查询|查看|列出|列表|完成|标记|删除|修改|更新|重开).{0,8}"
    r"(?:待办|任务|todo)",
    re.IGNORECASE,
)
_TODO_META_DISCUSSION_PATTERN = re.compile(
    r"(?:是不是|是否|不行|不能|失败|问题|怎么|为什么|还没|没法|无法|再改|改改)",
    re.IGNORECASE,
)
_TODO_EXPLICIT_REQUEST_PATTERN = re.compile(
    r"(?:请|帮我|给我|我要|记一个|记一下|创建一个|创建一条|新增一个|新建一个|建一个)",
    re.IGNORECASE,
)


def is_dingtalk_todo_create_intent(text: str) -> bool:
    """Return whether *text* is an explicit DingTalk Todo create request."""

    normalized = " ".join(text.strip().split())
    if not normalized:
        return False
    if not _TODO_CREATE_PATTERN.search(normalized):
        return False
    # A tester may quote the feature name while reporting a failure.  Only
    # treat such a sentence as creation when it also contains an imperative
    # request; this prevents “创建待办是不是还是不行” from creating data.
    if _TODO_META_DISCUSSION_PATTERN.search(normalized):
        return bool(_TODO_EXPLICIT_REQUEST_PATTERN.search(normalized))
    return True


def todo_create_tool_names(*, read_only: bool, is_todo_intent: bool) -> tuple[str, ...] | None:
    """Return the capability allowlist for a Todo creation invocation.

    ``None`` means the normal Direct Agent allowlist should be used.  Read-only
    invocations intentionally return an empty tuple: the caller must not expose
    a write tool while still preserving the Todo-specific prompt context.
    """

    if not is_todo_intent:
        return None
    if read_only:
        return ()
    return ("create_dingtalk_todo",)
