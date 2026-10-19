"""官方 QQ 回复分段：不超过 4 段，尾部合并。"""

from __future__ import annotations

from qq_bot.platform.reply import split_for_reply


def test_short_reply_kept_as_is() -> None:
    assert split_for_reply("你好\n\n今天吃白饭") == ["你好", "今天吃白饭"]


def test_empty_reply_falls_back_to_single_chunk() -> None:
    assert split_for_reply("   ") == [""]


def test_long_reply_merges_tail() -> None:
    text = "\n\n".join(f"第{i}段" for i in range(1, 8))
    chunks = split_for_reply(text)
    assert len(chunks) == 4
    assert chunks[0] == "第1段"
    assert "第7段" in chunks[-1]
