"""Deterministic routing for short, explicit DingTalk business actions.

The Direct Agent normally has access to workspace and Memory reads.  That is
useful for evidence-heavy questions, but it is the wrong execution surface for
an explicit calendar/todo request: the model can spend the whole run searching
for context before it ever reaches the requested DWS command.  Keep the
recognition conservative and use it only for imperative calendar creation.
"""

from __future__ import annotations

import re

from app.todo_routing import is_dingtalk_todo_create_intent


_CALENDAR_TERM_PATTERN = re.compile(
    r"(?:日历|日程|会议|会议室|calendar|meeting)",
    re.IGNORECASE,
)
_CALENDAR_CREATE_ACTION_PATTERN = re.compile(
    r"(?:"
    r"帮我|请|麻烦|给我|安排|约|预约|创建|新建|预订|订"
    r"|加(?:入|个|一个)?"
    r"|改期|取消|删除"
    r"|book|schedule|create|reschedule|cancel|delete"
    r")",
    re.IGNORECASE,
)
_CALENDAR_READ_ACTION_PATTERN = re.compile(
    r"(?:查询|查一下|查看|看看|今天|明天|本周|本月|日程表|闲忙|空闲|"
    r"list|show|check|freebusy)",
    re.IGNORECASE,
)
_CALENDAR_META_PATTERN = re.compile(
    r"(?:"
    r"(?:日历|日程|会议|会议室|calendar|meeting).{0,16}"
    r"(?:是不是|是否|为什么|怎么|失败|不行|有问题|出问题|报错|测试|排查)"
    r"|(?:是不是|是否|为什么|怎么|失败|不行|有问题|出问题|报错|测试|排查)"
    r".{0,16}(?:日历|日程|会议|会议室|calendar|meeting)"
    r")",
    re.IGNORECASE,
)
_TODO_ACTION_MARKER_PATTERN = re.compile(
    r"(?:记|创建|新建|新增|建|任务提醒|todo)",
    re.IGNORECASE,
)
_TODO_TERM_PATTERN = re.compile(r"(?:待办|todo)", re.IGNORECASE)


def _normalized(text: str) -> str:
    return " ".join(text.strip().split())


def is_dingtalk_calendar_create_intent(text: str) -> bool:
    """Return whether *text* explicitly asks to create/change a calendar item.

    Passive calendar cards often contain words such as ``日程`` and ``新增`` in
    their title.  They must not enter the write route, so this matcher requires
    an imperative marker and rejects troubleshooting/meta discussion.
    """

    normalized = _normalized(text)
    if not normalized:
        return False
    if not _CALENDAR_TERM_PATTERN.search(normalized):
        return False
    if not _CALENDAR_CREATE_ACTION_PATTERN.search(normalized):
        return False
    if _CALENDAR_META_PATTERN.search(normalized):
        return False
    # A calendar notification such as “日程：新增标注工具：……” has no
    # imperative request.  The action matcher intentionally excludes “新增”;
    # this additional guard documents the passive-card boundary explicitly.
    if normalized.startswith(("日程：", "日历：", "会议：")) and not re.search(
        r"(?:帮我|请|麻烦|给我|安排|约|预约|创建|新建|预订|订|加)",
        normalized,
        re.IGNORECASE,
    ):
        return False
    return True


def is_dingtalk_calendar_todo_composite_intent(text: str) -> bool:
    """Return whether one message explicitly requests both calendar and todo."""

    normalized = _normalized(text)
    return bool(
        normalized
        and is_dingtalk_calendar_create_intent(normalized)
        and (
            is_dingtalk_todo_create_intent(normalized)
            or (
                _TODO_TERM_PATTERN.search(normalized)
                and _TODO_ACTION_MARKER_PATTERN.search(normalized)
            )
        )
    )


def is_dingtalk_calendar_read_intent(text: str) -> bool:
    """Return whether *text* is an explicit calendar read/query request."""

    normalized = _normalized(text)
    return bool(
        normalized
        and _CALENDAR_TERM_PATTERN.search(normalized)
        and _CALENDAR_READ_ACTION_PATTERN.search(normalized)
        and not _CALENDAR_CREATE_ACTION_PATTERN.search(normalized)
    )
