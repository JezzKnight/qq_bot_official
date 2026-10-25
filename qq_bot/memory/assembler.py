"""上下文组装：群摘要 + 对话记录窗口 + 长期记忆注入。

设计：
- 群聊只保留**一个**对话记录窗口：同群最近 N 条（跨线程、按时间升序），不再区分
  「本人线程」和「群窗口」；每条的说话人由 ``<user identity>`` 标签区分。
- 私聊仍用个人线程窗口（私聊只有一个线程）。
- 群摘要由代码拼上「覆盖时间段」与「仅供参考」提醒，避免模型把旧摘要当成刚发生的事。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .token_est import estimate_tokens

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from datetime import datetime

    from qq_bot.ai.types import ChatMessage

    from .memory_config import MemoryConfig
    from .pg_repo import PgRepository

_TZ = ZoneInfo("Asia/Shanghai")
_NOTE_PREFIXES = ("[检索记录]", "[视频分析记录")
_MAX_NOTES = 3
SUMMARY_HINT = "（以上为群聊摘要，仅供参考；请自行判断是否与当前话题有关。）"


@dataclass
class ContextParts:
    conversation: list[ChatMessage] = field(default_factory=list)
    summary: str | None = None
    notes: str = ""
    stats: dict[str, int] = field(default_factory=dict)

    def as_history(self) -> list[ChatMessage]:
        return list(self.conversation)


_MIN_KEEP = 2


def _group_units(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """把 assistant(tool_calls) 与其后的 tool 结果捆成一个不可拆的单元。"""
    units: list[list[ChatMessage]] = []
    index = 0
    while index < len(messages):
        msg = messages[index]
        if msg.role == "assistant" and msg.tool_calls:
            unit = [msg]
            index += 1
            while index < len(messages) and messages[index].role == "tool":
                unit.append(messages[index])
                index += 1
            units.append(unit)
        else:
            units.append([msg])
            index += 1
    return units


def trim_by_budget(
    messages: list[ChatMessage], budget: int, *, keep_tail: int = _MIN_KEEP
) -> list[ChatMessage]:
    """按 token 预算从最旧裁，至少保留尾部 keep_tail 条；tool 调用对不拆开。"""
    units = _group_units(messages)
    total = sum(estimate_tokens(m.content) for m in messages)
    start = 0
    kept = len(messages)
    while total > budget and kept > keep_tail and start < len(units):
        unit = units[start]
        for item in unit:
            total -= estimate_tokens(item.content)
        kept -= len(unit)
        start += 1
    result = [item for unit in units[start:] for item in unit]
    # 兜底：开头若有孤立 tool 行（其 parent 不在窗口内），API 会 400，去掉。
    while result and result[0].role == "tool":
        result.pop(0)
    # 末尾若有 assistant(tool_calls) 却缺 tool 回复（被窗口截断），同样去掉。
    if result and result[-1].role == "assistant" and result[-1].tool_calls:
        result.pop()
    return result


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


def _is_note(content: str | None) -> bool:
    return (content or "").startswith(_NOTE_PREFIXES)


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return "未知"
    return value.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")


def format_summary(row: dict) -> str:
    """拼摘要正文：时间段 + 原文 + 仅供参考提醒。"""
    content = str(row.get("content") or "").strip()
    from_at = _fmt_time(row.get("covers_from_at"))
    to_at = _fmt_time(row.get("covers_to_at"))
    span = f"{from_at} 至 {to_at}"
    header = f"这段摘要发生的时间在<{span}>（北京时间）"
    return f"{header}\n{content}\n\n{SUMMARY_HINT}"


async def build_context(
    repo: PgRepository,
    *,
    thread_id: int,
    group_id: str | None,
    config: MemoryConfig,
    exclude_external_id: str | None = None,
) -> ContextParts:
    parts = ContextParts()
    if group_id is None:
        history = await repo.history(thread_id, config.short_term.thread_window)
        if exclude_external_id:
            history = [m for m in history if m.external_id != exclude_external_id]
        note_rows = [m for m in history if _is_note(m.content)]
        history = [m for m in history if not _is_note(m.content)]
        parts.notes = "\n\n".join(
            (m.content or "").strip() for m in note_rows[-_MAX_NOTES:]
        )
        parts.conversation = trim_by_budget(history, config.budget.thread_window)
        parts.stats = {"conversation": len(parts.conversation)}
        return parts

    rows = await repo.recent_group_messages(
        group_id, config.short_term.conversation_window
    )
    if exclude_external_id:
        rows = [m for m in rows if m.external_id != exclude_external_id]
    note_rows = [m for m in rows if _is_note(m.content)]
    rows = [m for m in rows if not _is_note(m.content)]
    parts.notes = "\n\n".join(
        (m.content or "").strip() for m in note_rows[-_MAX_NOTES:]
    )
    parts.conversation = trim_by_budget(rows, config.budget.conversation_window)
    summary_row = await repo.latest_summary_row(group_id)
    if summary_row:
        parts.summary = format_summary(summary_row)
    parts.stats = {
        "conversation": len(parts.conversation),
        "summary_tokens": estimate_tokens(parts.summary),
        "notes_tokens": estimate_tokens(parts.notes),
    }
    return parts
