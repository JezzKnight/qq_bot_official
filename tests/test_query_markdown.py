"""查询模式：Markdown 消息发送与回退。"""

from __future__ import annotations

import json
from typing import Any

from nonebot.adapters.qq.exception import ActionFailed
from nonebot.drivers import Response

from prompts.service import prompt_service
from qq_bot.platform.qqbot.reply import QQReply


class FakeBot:
    def __init__(self, *, fail_passive: bool = False) -> None:
        self.fail_passive = fail_passive
        self.passive: list[Any] = []
        self.active: list[Any] = []

    async def send(self, _event: Any, seg: Any) -> None:
        if self.fail_passive:
            raise ActionFailed(
                Response(400, content=json.dumps({"code": 40034007, "message": "no"}))
            )
        self.passive.append(seg)

    async def send_to_group(self, *, message: Any, **_: Any) -> None:
        self.active.append(message)


class FakeEvent:
    group_openid = "group-openid"


async def test_markdown_sends_one_passive_message() -> None:
    bot = FakeBot()
    await QQReply(bot, FakeEvent()).markdown("## 结论\n\n- 要点")
    assert len(bot.passive) == 1
    seg = bot.passive[0]
    assert seg.type == "markdown"
    assert seg.data["markdown"].content == "## 结论\n\n- 要点"
    assert not bot.active


async def test_markdown_falls_back_to_active() -> None:
    bot = FakeBot(fail_passive=True)
    await QQReply(bot, FakeEvent()).markdown("- 要点")
    assert len(bot.active) == 1
    assert bot.active[0].type == "markdown"


def test_query_prompt_is_markdown_oriented() -> None:
    text = prompt_service.get_query_prompt()
    assert "Markdown" in text
    assert "专有名词" in text
