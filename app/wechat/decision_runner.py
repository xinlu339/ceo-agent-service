"""Tool-free Pi runner for creating a WeChat reply decision."""
from __future__ import annotations

from app.agent_decision import AgentDecisionRunner
from app.pi_safety import make_read_only_without_tools


WECHAT_DECISION_DEVELOPER_INSTRUCTIONS = """You are a tool-free WeChat reply decision worker.

- Use only the supplied WeChat context. This invocation exposes no Friday Memory,
  durable-memory, DWS, or other external tools; do not call or claim to call them.
- Do not run tools, shell commands, or use web search, plugins, apps, DingTalk, Lark,
  browser, approval, document, mail, or messaging tools.
- Do not send, edit, approve, react, write memory, or otherwise cause an
  external side effect. The service persists the decision and owns delivery.
- Return only the requested AgentEnvelope JSON.
"""


class WechatDecisionRunner(AgentDecisionRunner):
    """A replay-safe decision step before the persisted WeChat delivery stage."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("approval_policy", "never")
        kwargs.setdefault("use_approval_bypass", False)
        kwargs.setdefault(
            "developer_instructions", WECHAT_DECISION_DEVELOPER_INSTRUCTIONS
        )
        kwargs.setdefault("command_mutator", make_read_only_without_tools)
        super().__init__(*args, **kwargs)
