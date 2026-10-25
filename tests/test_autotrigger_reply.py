"""自动接话的 ReplyTarget：表情包要真的发出去，而不是被丢掉。"""

from __future__ import annotations

import asyncio

from qq_bot.chat.autotrigger import ActiveReply


def test_active_reply_forwards_sticker() -> None:
    texts: list[str] = []
    images: list[tuple[str, bytes]] = []

    async def send_text(content: str) -> None:
        texts.append(content)

    async def send_image(data: bytes, name: str) -> None:
        images.append((name, data))

    reply = ActiveReply(send_text, send_image)

    async def run() -> None:
        await reply.text("来了")
        await reply.image(b"gif-bytes", "token来")

    asyncio.run(run())
    assert texts == ["来了"]
    assert images == [("token来", b"gif-bytes")]


def test_active_reply_forwards_markdown() -> None:
    texts: list[str] = []
    markdowns: list[str] = []

    async def send_text(content: str) -> None:
        texts.append(content)

    async def send_markdown(content: str) -> None:
        markdowns.append(content)

    reply = ActiveReply(send_text, send_markdown=send_markdown)

    async def run() -> None:
        await reply.markdown("## 结论\n内容")

    asyncio.run(run())
    assert texts == []
    assert markdowns == ["## 结论\n内容"]


def test_active_reply_markdown_falls_back_to_text() -> None:
    texts: list[str] = []

    async def send_text(content: str) -> None:
        texts.append(content)

    reply = ActiveReply(send_text)

    async def run() -> None:
        await reply.markdown("## 结论")

    asyncio.run(run())
    assert texts == ["## 结论"]


def test_active_reply_without_image_sender_is_noop() -> None:
    reply = ActiveReply(_noop)

    async def run() -> None:
        await reply.image(b"x", "无语")

    asyncio.run(run())  # 不抛异常即可


async def _noop(_content: str) -> None:  # pragma: no cover - 只为构造对象
    return None


def test_active_reply_allows_up_to_four_parts() -> None:
    from qq_bot.chat.autotrigger import ActiveReply

    assert ActiveReply.max_parts == 4
