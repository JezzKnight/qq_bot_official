"""群摘要压缩：轮次触发 + advisory lock + 幂等写入。

只负责"什么时候压、压哪些、怎么写"，摘要文本由调用方注入：
- 生产：plugins 层用模型生成（memory_compress.py）
- 测试：传一个假实现即可，不需要真的调模型
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .token_est import estimate_tokens

if TYPE_CHECKING:
    from .memory_config import MemoryConfig
    from .pg_repo import PgRepository

# (已有摘要, 待压缩消息, 摘要 token 上限) -> 摘要正文
SummaryFn = Callable[[str | None, list[dict], int], Awaitable[str]]

_LOCK_NAMESPACE = "memory_compress"
_IDENTITY_TAG = re.compile(r"<user identity[^>]*/>\s*")
_REBUILD_LIMIT = 300  # 全量重建时回看的最多消息数，避免无界扫描


@dataclass(frozen=True)
class CompressOutcome:
    covers_from: int
    covers_to: int
    messages: int
    tokens: int
    rebuilt: bool = False


def format_transcript(messages: list[dict]) -> str:
    """把消息转成「发言人: 内容」的纯文本；去掉身份标签，节省 token。"""
    lines: list[str] = []
    for msg in messages:
        content = _IDENTITY_TAG.sub("", msg.get("content") or "").strip()
        if not content:
            continue
        if msg["role"] == "assistant":
            lines.append(f"bot: {content}")
        elif msg["role"] == "user":
            lines.append(f"{msg.get('sender_name') or '某成员'}: {content}")
    return "\n".join(lines)


async def compress_if_needed(
    repo: PgRepository,
    *,
    group_id: int,
    config: MemoryConfig,
    summarize: SummaryFn,
) -> CompressOutcome | None:
    """按轮次触发压缩；未达阈值/无待压内容/拿不到锁时返回 None。"""
    last_covered = await repo.latest_covers_to(group_id)
    pending_turns = await repo.count_user_messages_since(group_id, last_covered)
    if pending_turns < config.compression.trigger_turns:
        return None

    # 窗口内保持原文；窗口边界在锁内再校验一次
    cutoff = await repo.cutoff_for_window(group_id, config.short_term.group_window)
    if cutoff <= last_covered:
        return None

    async with repo.pool.acquire() as conn:
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1::text), hashtext($2::text))",
            _LOCK_NAMESPACE,
            str(group_id),
        )
        if not locked:
            return None
        try:
            return await _compress_locked(
                repo,
                group_id=group_id,
                cutoff=cutoff,
                config=config,
                summarize=summarize,
            )
        finally:
            await conn.fetchval(
                "SELECT pg_advisory_unlock(hashtext($1::text), hashtext($2::text))",
                _LOCK_NAMESPACE,
                str(group_id),
            )


async def _compress_locked(
    repo: PgRepository,
    *,
    group_id: int,
    cutoff: int,
    config: MemoryConfig,
    summarize: SummaryFn,
) -> CompressOutcome | None:
    """持锁后的实际压缩：重读游标 → 取批次 → 摘要 → 幂等写入。"""
    last_covered = await repo.latest_covers_to(group_id)
    if cutoff <= last_covered:
        return None

    rebuilt = False
    summary_count = await repo.count_summaries(group_id)
    if summary_count >= config.compression.full_rebuild_every:
        # 全量重建：用最近若干原文重写，抵消多次增量累积的损耗
        messages = await repo.group_messages_for_rebuild(
            group_id, cutoff, _REBUILD_LIMIT
        )
        previous = None
        rebuilt = True
    else:
        messages = await repo.group_messages_between(group_id, last_covered, cutoff)
        previous = await repo.latest_summary(group_id)

    if not messages:
        return None

    raw = await summarize(
        previous, messages, config.compression.summary_max_tokens
    )
    content = raw.strip()
    if not content:
        return None

    covers_from = 1 if rebuilt else messages[0]["id"]
    await repo.insert_summary(
        group_id=group_id,
        content=content,
        covers_from=covers_from,
        covers_to=messages[-1]["id"],
    )
    return CompressOutcome(
        covers_from=covers_from,
        covers_to=messages[-1]["id"],
        messages=len(messages),
        tokens=estimate_tokens(content),
        rebuilt=rebuilt,
    )
