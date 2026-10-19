"""一轮对话的 token 用量累加（ContextVar）。

为什么按轮累加而不是按单次调用：一次回复可能包含工具循环的多次 LLM 调用，
以及工具内部子 Agent 的调用；用户关心的是"这条回复花了多少"，所以统一累加，
在落库时写到该轮的 assistant 消息上（见 v005）。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class TurnUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0


current_turn_usage: contextvars.ContextVar[TurnUsage | None] = contextvars.ContextVar(
    "current_turn_usage", default=None
)


def reset() -> TurnUsage:
    """每轮开始时重置。"""
    usage = TurnUsage()
    current_turn_usage.set(usage)
    return usage


def add(prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> None:
    """累加一次 LLM 调用；未初始化时自动初始化（子 Agent 可能在工具里独立记录）。"""
    usage = current_turn_usage.get()
    if usage is None:
        usage = reset()
    usage.prompt_tokens += max(int(prompt_tokens), 0)
    usage.cached_tokens += max(int(cached_tokens), 0)
    usage.completion_tokens += max(int(completion_tokens), 0)
    usage.calls += 1


def snapshot() -> TurnUsage:
    return current_turn_usage.get() or TurnUsage()
