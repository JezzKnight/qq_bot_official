"""QQ 官方回复通道：首条被动回复，后续/表情走主动消息（避免每条都带 @）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nonebot.adapters.qq import MessageSegment

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 25.0


class QQReply:
    """实现 ReplyTarget；官方每条被动消息最多回复 5 次，这里保守取 4 段。

    平台会把"带 msg_id 的被动回复"显示成对原消息的回复（客户端表现为 @ 对方）。
    因此只有首条走被动回复，后续分段与表情包走主动消息，避免每段都带 @。
    """

    max_parts = 4

    def __init__(self, bot: Any, event: Any) -> None:
        self._bot = bot
        self._event = event

    async def _send(self, seg: Any, *, passive: bool) -> None:
        logger.info("QQ send start passive=%s", passive)
        try:
            await asyncio.wait_for(self._send_once(seg, passive=passive), SEND_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("QQ send timeout after %ss passive=%s", SEND_TIMEOUT, passive)
            raise
        logger.info("QQ send done passive=%s", passive)

    async def _send_once(self, seg: Any, *, passive: bool) -> None:
        if passive:
            await self._bot.send(self._event, seg)
            return
        group_openid = getattr(self._event, "group_openid", None)
        if not group_openid:
            # 私聊没有"每段 @ 人"的问题，保持被动回复，避免占用主动消息额度
            await self._bot.send(self._event, seg)
            return
        try:
            await self._bot.send_to_group(group_openid=group_openid, message=seg)
        except Exception:
            # 主动消息可能被用户关闭或超额：退回被动回复，至少保证送达
            logger.warning("QQ 主动消息发送失败，回退被动回复", exc_info=True)
            await self._bot.send(self._event, seg)

    async def text(self, content: str) -> None:
        await self._send(MessageSegment.text(content), passive=False)

    async def passive(self, content: str) -> None:
        """指令回复：单条被动回复，显示为对原消息的回复。"""
        await self._send(MessageSegment.text(content), passive=True)

    async def mention(self, user_key: str, content: str) -> None:  # noqa: ARG002
        """首条回复：被动回复即可，平台会自动关联原消息。"""
        await self._send(MessageSegment.text(content), passive=True)

    async def image(self, data: bytes, name: str = "") -> None:
        seg = MessageSegment.file_image(data, name or "image.png")
        await self._send(seg, passive=False)

    async def finish(self) -> None:
        """官方适配器没有 finish 语义，回复发送完即可返回。"""
