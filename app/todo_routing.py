"""Deterministic routing helpers for DingTalk Todo creation requests."""

from __future__ import annotations

import re


# Keep this matcher deliberately focused on creation.  Query and mutation
# requests such as "查待办" or "标记待办完成" must continue through the normal
# agent path because they have different commands and confirmation semantics.
_TODO_CREATE_PATTERN = re.compile(
    r"(?:"
    r"记(?:一个|一下|一条|个|条)?\s*(?:钉钉)?待办"
    r"|创建(?:一个|一条|个)?\s*(?:钉钉)?待办"
    r"|新增(?:一个|一条|个)?\s*(?:钉钉)?待办"
    r"|新建(?:一个|一条|个)?\s*(?:钉钉)?待办"
    r"|建(?:一个|一条|个)?\s*(?:钉钉)?待办"
    r"|(?:帮我)?记(?:一下|一个|一条|个)?任务"
    r"|任务提醒"
    r"|\bTODO\b"
    r")",
    re.IGNORECASE,
)
_TODO_META_DISCUSSION_PATTERN = re.compile(
    r"(?:"
    r"(?:待办|todo).{0,12}(?:是不是|是否|不行|不能|失败|还没|没法|无法|再改|改改)"
    r"|(?:是不是|是否|不行|不能|失败|还没|没法|无法|再改|改改).{0,12}(?:待办|todo)"
    r"|(?:待办|todo).{0,8}(?:有问题|出问题|的问题|怎么|为什么)"
    r")",
    re.IGNORECASE,
)


def is_dingtalk_todo_create_intent(text: str) -> bool:
    """Return whether *text* is an explicit DingTalk Todo create request."""

    normalized = " ".join(text.strip().split())
    if not normalized:
        return False
    if not _TODO_CREATE_PATTERN.search(normalized):
        return False
    # A tester may quote the feature name while reporting a failure.  Do not
    # treat meta/problem discussion such as “创建待办是不是还是不行” as a
    # real write request, so reporting a failed test cannot create data.
    if _TODO_META_DISCUSSION_PATTERN.search(normalized):
        return False
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
