"""落库注脚：检索记录必须进历史，否则下一轮模型会把查过的当成编的。"""

from __future__ import annotations

from qq_bot.chat.turn import _clip_excerpt, _extra_notes, _strip_echoed_notes


def test_search_note_only() -> None:
    note = _extra_notes("", ["CS2 2026年10月 赛事日程 比赛"])
    assert note is not None
    assert "[检索记录]" in note
    assert "CS2 2026年10月 赛事日程 比赛" in note


def test_video_and_search_notes_combined() -> None:
    note = _extra_notes("视频里在讲 Rust", ["比比拉布 什么意思 梗"])
    assert note is not None
    assert "[视频分析记录" in note
    assert "[检索记录]" in note


def test_search_excerpt_kept_for_later_check() -> None:
    note = _extra_notes(
        "",
        ["PGL Masters Bucharest 2026 CS2 参赛队伍 名单"],
        "Vitality 阵容：apEX 缺席 由 jL 替补",
    )
    assert note is not None
    assert "摘录：Vitality 阵容：apEX 缺席 由 jL 替补" in note


def test_excerpt_is_flattened_and_clipped() -> None:
    excerpt = _clip_excerpt("第一行\n\n第二行 " + "长" * 300)
    assert "\n" not in excerpt
    assert excerpt.endswith("…")
    assert len(excerpt) <= 201


def test_no_notes_returns_none() -> None:
    assert _extra_notes("", []) is None


def test_duplicate_queries_merged() -> None:
    note = _extra_notes("", ["CS2 赛事", "CS2 赛事", "CS2 赛事（补查）"])
    assert note is not None
    assert note.count("CS2 赛事；") == 1


# ── 系统台账：检索记录进提示词，不进对话历史；模型复述时要剥掉 ──


def test_strip_echoed_note_before_answer() -> None:
    text = "[检索记录] 这条回复查过网：X\n摘录：Y\n\n真正的回答"
    assert _strip_echoed_notes(text) == "真正的回答"


def test_strip_echoed_video_note() -> None:
    text = "[视频分析记录，仅供后续对话参考]\n内容\n\n回答"
    assert _strip_echoed_notes(text) == "回答"


def test_strip_echoed_notes_keeps_plain_answer() -> None:
    assert _strip_echoed_notes("正常回答") == "正常回答"


def test_strip_echoed_notes_without_separator() -> None:
    assert _strip_echoed_notes("[检索记录] 只有台账") == ""
