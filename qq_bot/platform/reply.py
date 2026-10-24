"""回复文本的分条与发送节奏（官方 QQ 被动回复上限 5 次）。

三层职责：

1. ``split_for_reply`` —— 只按空行（``\n\n``）分条：模型自己决定怎么分，最多 4 条；
   代码不再按长度/标点二次切分，避免把一句自然的话切碎。
2. ``typing_delay`` —— 按字数估算"打完这句话"要多久，作为下一条消息的停顿。
3. ``send_reply_chunks`` —— 首条立即发（群聊带 @），其后每条前停顿。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_bot.platform.ports import ReplyTarget

logger = logging.getLogger(__name__)

MAX_REPLY_PARTS = 4  # 官方被动回复 5 次，保守取 4，留出表情包的额度
TYPE_DELAY_BASE = 0.35  # 每条消息的"抬手"时间（秒）
TYPE_DELAY_PER_CHAR = 0.07  # 每个字的打字时间（秒/字）
TYPE_DELAY_CAP = 1.8  # 单条最长停顿，避免 4 段拖成十秒


def split_for_reply(text: str, *, max_parts: int = MAX_REPLY_PARTS) -> list[str]:
    """按空行分条；超过 ``max_parts`` 时把尾部并进最后一条。"""
    parts = [
        part.strip()
        for part in (text or "").replace("\r\n", "\n").split("\n\n")
        if part.strip()
    ]
    if not parts:
        return [""]
    if len(parts) <= max_parts:
        return parts
    head = parts[: max_parts - 1]
    head.append("\n".join(parts[max_parts - 1 :]))
    return head


def typing_delay(text: str) -> float:
    """按字数估算"打完这句话"需要的停顿（秒），供分段发送使用。"""
    if not text.strip():
        return 0.0
    return min(TYPE_DELAY_CAP, TYPE_DELAY_BASE + TYPE_DELAY_PER_CHAR * len(text))


async def send_reply_chunks(
    reply: ReplyTarget,
    chunks: list[str],
    *,
    mention_user_key: str | None = None,
    simulate_typing: bool = True,
    fallback_to_text: bool = False,
) -> None:
    """逐段发送：首段立即（群聊走被动回复带 @），其后按打字时间停顿。

    ``mention_user_key`` 为 None 时不带 @（私聊）；
    ``fallback_to_text`` 为 True 时，首段 @ 发送失败会退回普通消息（后台视频回复）。
    """
    pending = [chunk for chunk in chunks if chunk.strip()]
    for idx, chunk in enumerate(pending):
        if idx == 0:
            await _send_first(
                reply,
                chunk,
                mention_user_key=mention_user_key,
                fallback_to_text=fallback_to_text,
            )
            continue
        if simulate_typing:
            await asyncio.sleep(typing_delay(chunk))
        await reply.text(chunk)


async def _send_first(
    reply: ReplyTarget,
    chunk: str,
    *,
    mention_user_key: str | None,
    fallback_to_text: bool,
) -> None:
    if mention_user_key is None:
        await reply.text(chunk)
        return
    try:
        await reply.mention(mention_user_key, chunk)
    except Exception:
        if not fallback_to_text:
            raise
        logger.warning("首段 @ 发送失败，改走普通消息", exc_info=True)
        await reply.text(chunk)
