"""QQ 回复通道：图片内容违规时发一次提示并停止批量发送。"""

from __future__ import annotations

import json
from typing import Any

from nonebot.adapters.qq.exception import ActionFailed
from nonebot.drivers import Response

from qq_bot.platform.qqbot.reply import (
    CONTENT_VIOLATION_NOTICE,
    QQReply,
    normalize_typing,
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.image_attempts = 0

    async def send(self, _event: Any, seg: Any) -> None:
        if seg.type != "text":
            raise ActionFailed(
                Response(
                    400,
                    content=json.dumps({"code": 40034006, "message": "消息内容违规"}),
                )
            )
        self.sent.append(seg)

    async def send_to_group(self, *, message: Any, **_: Any) -> None:
        if message.type != "text":
            self.image_attempts += 1
            raise ActionFailed(
                Response(
                    400,
                    content=json.dumps({"code": 40034006, "message": "消息内容违规"}),
                )
            )
        self.sent.append(message)


class FakeEvent:
    group_openid = "group-openid"


async def test_image_content_violation_sends_notice_once() -> None:
    bot = FakeBot()
    reply = QQReply(bot, FakeEvent())
    assert await reply.image(b"fake-image", "a.png") is False
    assert await reply.image(b"fake-image", "b.png") is False
    assert bot.image_attempts == 1
    texts = [seg.data.get("text") for seg in bot.sent if seg.type == "text"]
    assert texts == [normalize_typing(CONTENT_VIOLATION_NOTICE)]


def test_normalize_typing_uses_space_for_chinese_comma() -> None:
    assert normalize_typing("一点还不睡，明天上班不困成狗啊") == (
        "一点还不睡 明天上班不困成狗啊"
    )
    assert normalize_typing("代码里的 foo(a, b) 不动") == "代码里的 foo(a, b) 不动"
