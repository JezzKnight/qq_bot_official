"""三层上下文组装：群摘要 + 群窗口 + 个人线程窗口。

设计见 docs/memory-design.md §3/§6：
- 群窗口**排除本人线程**，与个人线程窗口互不重叠；
- 每层独立 token 预算，超预算只裁本层（从最旧裁）；
- 摘要段在 P2 接入，缺失时为空。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .token_est import estimate_tokens

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from qq_bot.ai.types import ChatMessage

    from .memory_config import MemoryConfig
    from .pg_repo import PgRepository


@dataclass
class ContextParts:
    group_window: list[ChatMessage] = field(default_factory=list)
    thread_window: list[ChatMessage] = field(default_factory=list)
    summary: str | None = None
    stats: dict[str, int] = field(default_factory=dict)

    def as_history(self) -> list[ChatMessage]:
        """注入顺序：群窗口在前（他人），个人线程在后（本人）。"""
        return [*self.group_window, *self.thread_window]


_MIN_KEEP = 2


def trim_by_budget(
    messages: list[ChatMessage], budget: int, *, keep_tail: int = _MIN_KEEP
) -> list[ChatMessage]:
    """按 token 预算从最旧裁，至少保留尾部 keep_tail 条。"""
    result = list(messages)
    total = sum(estimate_tokens(m.content) for m in result)
    while total > budget and len(result) > keep_tail:
        total -= estimate_tokens(result[0].content)
        result.pop(0)
    return result


async def build_context(
    repo: PgRepository,
    *,
    thread_id: int,
    group_id: str | None,
    config: MemoryConfig,
    exclude_external_id: str | None = None,
) -> ContextParts:
    parts = ContextParts()
    thread = await repo.history(thread_id, config.short_term.thread_window)
    if exclude_external_id:
        thread = [m for m in thread if m.external_id != exclude_external_id]
    parts.thread_window = trim_by_budget(
        thread,
        config.budget.thread_window,
    )
    if group_id is not None:
        parts.group_window = trim_by_budget(
            await repo.group_window(
                group_id, thread_id, config.short_term.group_window
            ),
            config.budget.group_window,
        )
        parts.summary = await repo.latest_summary(group_id)
    parts.stats = {
        "group_window": len(parts.group_window),
        "thread_window": len(parts.thread_window),
        "summary_tokens": estimate_tokens(parts.summary),
    }
    return parts


def trim_messages(messages: list[ChatMessage], budget: int) -> list[ChatMessage]:
    """整段请求的最终兜底裁剪：保留索引 0（system），从最旧对话开始裁。"""
    if len(messages) <= _MIN_KEEP:
        return messages
    result = list(messages)
    before = len(result)
    total = sum(estimate_tokens(m.content) for m in result)
    while total > budget and len(result) > _MIN_KEEP:
        total -= estimate_tokens(result[1].content)
        result.pop(1)
    if len(result) < before:
        logger.warning(
            "上下文超出预算，裁剪 %d 条消息（%d -> %d，估算 %d tokens，预算 %d）",
            before - len(result),
            before,
            len(result),
            total,
            budget,
        )
    return result
