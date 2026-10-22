"""上下文预算回归：大 system prompt 不能把个人线程全部裁掉。"""

from __future__ import annotations

from qq_bot.ai.types import ChatMessage
from qq_bot.memory.assembler import trim_messages
from qq_bot.memory.token_est import estimate_tokens


def test_estimate_tokens_does_not_double_count_chinese() -> None:
    # 旧实现按 2 token/汉字估算，会把 5k 字的 system prompt 估成 1 万 token，
    # 导致 trim_messages 误删整段个人线程。
    assert estimate_tokens("中" * 1000) == 1000


def test_trim_keeps_thread_with_large_system_prompt() -> None:
    system = ChatMessage(role="system", content="规则" * 2500)
    history = [
        ChatMessage(role="user" if i % 2 == 0 else "assistant", content="消息" * 10)
        for i in range(12)
    ]
    messages = [system, *history, ChatMessage(role="user", content="暗猫是谁？")]
    assert trim_messages(messages, 8192) == messages
