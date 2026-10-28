"""引用回复：<quote m="..."/> 短别名解析、生成与发送通道。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from qq_bot.chat import quote as quote_mod
from qq_bot.chat.autotrigger import ActiveReply
from qq_bot.platform.qqbot.reply import QQReply


def test_parse_alias_with_two_paragraphs() -> None:
    parsed = quote_mod.parse_quoted_reply(
        '<quote m="k3f9"/>给口？你这是打赏还是光给我画个饼\n\ntoken来 token来'
    )
    assert parsed is not None
    assert parsed.alias == "k3f9"
    assert parsed.quoted_body == "给口？你这是打赏还是光给我画个饼"
    assert parsed.rest_body == "token来 token来"


def test_parse_alias_only_paragraph() -> None:
    parsed = quote_mod.parse_quoted_reply('<quote m="k3f9"/>这话我接一下')
    assert parsed is not None
    assert parsed.rest_body == ""


def test_parse_alias_is_lowercased() -> None:
    parsed = quote_mod.parse_quoted_reply('<quote m="K3F9"/>接一句')
    assert parsed is not None
    assert parsed.alias == "k3f9"


def test_parse_without_tag_returns_none() -> None:
    assert quote_mod.parse_quoted_reply("普通回复") is None


def test_parse_tag_not_at_start_returns_none() -> None:
    assert quote_mod.parse_quoted_reply('先说一句 <quote m="k3f9"/> 再说') is None


def test_parse_tag_without_body_returns_none() -> None:
    assert quote_mod.parse_quoted_reply('<quote m="k3f9"/>') is None


def test_parse_legacy_text_snippet_is_not_supported() -> None:
    # 只认短别名；文本片段格式不再兜底
    assert quote_mod.parse_quoted_reply("<quote>给口</quote>接一句") is None


def test_make_alias_shape_and_uniqueness() -> None:
    taken: set[str] = set()
    for _ in range(50):
        alias = quote_mod.make_alias(taken)
        assert len(alias) == quote_mod.ALIAS_LENGTH
        assert set(alias) <= set(quote_mod.ALIAS_ALPHABET)
        assert alias not in taken
        taken.add(alias)


def test_make_alias_skips_taken() -> None:
    taken = {"k3f9"}
    alias = quote_mod.make_alias(taken)
    assert alias != "k3f9"


def test_qq_reply_quote_sends_reference() -> None:
    sent: list[object] = []

    class FakeBot:
        async def send_to_group(self, **kwargs: object) -> None:
            sent.append(kwargs["message"])

    event = SimpleNamespace(group_openid="g1")
    reply = QQReply(FakeBot(), event)
    asyncio.run(reply.quote("msg-id", "给口？你这是打赏"))

    assert len(sent) == 1
    segments = list(sent[0])
    assert segments[0].type == "reference"
    assert segments[0].data["reference"].message_id == "msg-id"
    assert "给口" in segments[1].data["text"]


def test_qq_reply_quote_falls_back_to_text() -> None:
    texts: list[str] = []

    class FakeBot:
        async def send_to_group(self, **_kwargs: object) -> None:
            raise RuntimeError

        async def send(self, *_args: object) -> None:
            texts.append(_args[1].data["text"])

    event = SimpleNamespace(group_openid="g1")
    reply = QQReply(FakeBot(), event)
    asyncio.run(reply.quote("msg-id", "降级文本"))
    assert any("降级文本" in text for text in texts)


def test_active_reply_quote_forwarding() -> None:
    quotes: list[tuple[str, str]] = []

    async def send_text(_content: str) -> None:
        return None

    async def send_quote(message_id: str, content: str) -> None:
        quotes.append((message_id, content))

    reply = ActiveReply(send_text, send_quote=send_quote)
    asyncio.run(reply.quote("m1", "引用内容"))
    assert quotes == [("m1", "引用内容")]


def test_active_reply_quote_falls_back_to_text() -> None:
    texts: list[str] = []

    async def send_text(content: str) -> None:
        texts.append(content)

    reply = ActiveReply(send_text)
    asyncio.run(reply.quote("m1", "没有引用通道"))
    assert texts == ["没有引用通道"]


def test_needs_auto_quote_only_when_newer_message() -> None:
    base = {
        "enabled": True,
        "is_group": True,
        "auto_reply": True,
        "trigger_mid": "m1",
        "latest_mid": "m2",
    }
    assert quote_mod.needs_auto_quote(**base)
    assert not quote_mod.needs_auto_quote(**{**base, "latest_mid": "m1"})
    assert not quote_mod.needs_auto_quote(**{**base, "latest_mid": ""})
    assert not quote_mod.needs_auto_quote(**{**base, "auto_reply": False})
    assert not quote_mod.needs_auto_quote(**{**base, "is_group": False})
    assert not quote_mod.needs_auto_quote(**{**base, "enabled": False})
