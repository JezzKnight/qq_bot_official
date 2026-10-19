"""用量记录：累加到当前轮的 TurnUsage（历史实现是独立 SQLite 日聚合表）。

现在用量随 assistant 消息落库（messages 的 prompt/completion/cached/llm_calls 字段），
``/usage`` 直接从 messages 聚合。保留本模块是为了让所有调用方（主循环与子 Agent）
继续用同一个入口 ``record``。
"""

from __future__ import annotations

from qq_bot.ai.usage_context import add


async def record(
    prompt_tokens: int = 0,
    cached_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """记录一次 LLM 调用的用量（累加到本轮）。"""
    add(prompt_tokens, cached_tokens, completion_tokens)
