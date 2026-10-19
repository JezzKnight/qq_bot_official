"""OneBot 回复通道：把 ReplyTarget 协议映射到 matcher。"""

from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment


class OneBotReply:
    """实现 ReplyTarget；OneBot 对单条消息的回复次数没有平台限制。"""

    max_parts = 1000

    def __init__(self, matcher: Any) -> None:
        self._matcher = matcher

    async def text(self, content: str) -> None:
        if content:
            await self._matcher.send(MessageSegment.text(content))

    async def mention(self, user_key: str, content: str) -> None:
        await self._matcher.send(
            MessageSegment.at(int(user_key)) + MessageSegment.text("\n" + content)
        )

    async def image(self, data: bytes, name: str = "") -> None:  # noqa: ARG002
        await self._matcher.send(MessageSegment.image(data))

    async def finish(self) -> None:
        await self._matcher.finish()
