"""Channel-specific prompt for WeChat turns.

WeChat replies are plain text and use only the supplied same-conversation
context. The agent must return the existing AgentEnvelope and must not request
DingTalk-only system actions.
"""
from __future__ import annotations

from app.wechat.models import WechatMessage

WECHAT_TURN_INSTRUCTIONS = """- This is a selected personal WeChat conversation.
- Use only the supplied same-conversation context. This invocation exposes no tools; do not call or claim to call Friday Memory, DWS, or any other tool.
- If essential durable history is absent, ask one concise clarifying question or hand off instead of inventing it.
- Return only the existing AgentEnvelope.
- Allowed user modes: send_reply, ask_clarifying_question, handoff_to_human, no_reply.
- Do not request DingTalk-only system actions, reactions, documents, OA, calendar, or DING.
- Group context that did not mention the principal is background only."""


def build_wechat_turn_prompt(
    trigger: WechatMessage, context: list[WechatMessage]
) -> str:
    lines = [WECHAT_TURN_INSTRUCTIONS, "", "同一对话最近上下文（最多 20 条）:"]
    for message in context[-20:]:
        lines.append(f"[{message.sent_at}] {message.sender_display_name}: {message.text}")
    lines += [
        "",
        "需要处理的触发消息:",
        f"[{trigger.sent_at}] {trigger.sender_display_name}: {trigger.text}",
    ]
    return "\n".join(lines)
