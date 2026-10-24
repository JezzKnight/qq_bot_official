"""官方 QQ 回复分段：不超过 4 段，尾部合并；长句按语义断点切条。"""

from __future__ import annotations

from qq_bot.platform.reply import (
    TYPE_DELAY_CAP,
    send_reply_chunks,
    split_for_reply,
    typing_delay,
)


def test_short_reply_kept_as_is() -> None:
    assert split_for_reply("你好\n\n今天吃白饭") == ["你好", "今天吃白饭"]


def test_single_newline_also_splits() -> None:
    # 模型常用单换行分段：短句也要切成两条
    assert split_for_reply("螶鋒嗰體獽彎欎\n打一遍我手都累了") == [
        "螶鋒嗰體獽彎欎",
        "打一遍我手都累了",
    ]


def test_crlf_splits_too() -> None:
    assert split_for_reply("甲\r\n乙") == ["甲", "乙"]


def test_empty_reply_falls_back_to_single_chunk() -> None:
    assert split_for_reply("   ") == [""]


def test_long_reply_merges_tail() -> None:
    text = "\n\n".join(f"第{i}段" for i in range(1, 8))
    chunks = split_for_reply(text)
    assert len(chunks) == 4
    assert chunks[0] == "第1段"


def test_long_line_splits_on_spaces() -> None:
    """模型常常写成一大段（我们的风格用空格断句），超长要按空格切开。"""
    text = (
        "等下 是哪条 你说 我核一遍 要是真是我给错了 我现在就认 对不起 "
        "你气的是给错数还不吭声吧 换我也烦"
    )
    chunks = split_for_reply(text, max_chars=20)
    assert len(chunks) > 1
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert " ".join(chunks) == text


def test_sentence_end_preferred_over_space() -> None:
    chunks = split_for_reply("第一句结束。第二句也结束。第三句", max_chars=8)
    assert chunks[0].endswith("。")


def test_never_splits_inside_number() -> None:
    """500N / 1612N 这类是技术回答的命根子，不能被劈开。"""
    text = "实测是 1612N 断裂 厂家给的单台总成静载只有 2000N 这个差值你得看看"
    chunks = split_for_reply(text, max_chars=10)
    assert "1612N" in " ".join(chunks)
    assert "2000N" in " ".join(chunks)


def test_does_not_break_after_dangling_word() -> None:
    """断在"是/的/了"这类单字后面会把一句话读成半句。"""
    text = "我查了一圈 GB 7258-2017 的 7.2.9 是 500N 那是操作力 不是结构强度"
    chunks = split_for_reply(text, max_chars=30)
    assert not chunks[0].endswith(("是", "的", "了"))
    assert chunks[1].startswith("是 ")


def test_zero_max_chars_disables_splitting() -> None:
    text = "很长的一段话" * 10
    assert split_for_reply(text, max_chars=0) == [text]


def test_max_parts_holds_after_splitting() -> None:
    chunks = split_for_reply("很短的话 和别的问题 " * 20, max_parts=3, max_chars=10)
    assert len(chunks) == 3


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
