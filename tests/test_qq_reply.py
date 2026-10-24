"""官方 QQ 回复分条：只按空行切，最多 4 条，尾部合并。"""

from __future__ import annotations

from qq_bot.platform.reply import (
    TYPE_DELAY_CAP,
    send_reply_chunks,
    split_for_reply,
    typing_delay,
)


def test_blank_line_splits_into_messages() -> None:
    assert split_for_reply("你好\n\n今天吃白饭") == ["你好", "今天吃白饭"]


def test_single_newline_does_not_split() -> None:
    # 单个换行只是换行；分条只认空行
    text = "螶鋒嗰體獽彎欎\n打一遍我手都累了"
    assert split_for_reply(text) == [text]


def test_crlf_blank_line_splits() -> None:
    assert split_for_reply("甲\r\n\r\n乙") == ["甲", "乙"]


def test_empty_reply_falls_back_to_single_chunk() -> None:
    assert split_for_reply("   ") == [""]


def test_long_paragraph_is_not_split_by_length() -> None:
    text = "很长的一段话" * 30
    assert split_for_reply(text) == [text]


def test_more_than_max_parts_merges_tail() -> None:
    text = "\n\n".join(f"第{i}段" for i in range(1, 8))
    chunks = split_for_reply(text)
    assert len(chunks) == 4
    assert chunks[:3] == ["第1段", "第2段", "第3段"]
    assert chunks[3] == "\n".join(f"第{i}段" for i in range(4, 8))


def test_max_parts_one_merges_everything() -> None:
    assert split_for_reply("甲\n\n乙\n\n丙", max_parts=1) == ["甲\n乙\n丙"]


class FakeReply:
    max_parts = 4

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def text(self, content: str) -> None:
        self.sent.append(("text", content))

    async def mention(self, user_key: str, content: str) -> None:
        self.sent.append(("mention", f"{user_key}|{content}"))


async def test_send_reply_chunks_first_mentioned_rest_plain() -> None:
    reply = FakeReply()
    await send_reply_chunks(
        reply, ["甲", "乙"], mention_user_key="u1", simulate_typing=False
    )
    assert reply.sent == [("mention", "u1|甲"), ("text", "乙")]


async def test_send_reply_chunks_without_mention_starts_with_text() -> None:
    reply = FakeReply()
    await send_reply_chunks(reply, ["甲", "乙"], simulate_typing=False)
    assert reply.sent == [("text", "甲"), ("text", "乙")]


async def test_send_reply_chunks_skips_blank() -> None:
    reply = FakeReply()
    await send_reply_chunks(reply, ["", "  ", "甲"], simulate_typing=False)
    assert reply.sent == [("text", "甲")]


def test_typing_delay_scales_with_length_and_caps() -> None:
    assert typing_delay("") == 0.0
    assert typing_delay("短") < typing_delay("这句话稍微长一点")
    assert typing_delay("很" * 100) == TYPE_DELAY_CAP
