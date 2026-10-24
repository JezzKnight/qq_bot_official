"""落库注脚：检索记录必须进历史，否则下一轮模型会把查过的当成编的。"""

from __future__ import annotations

from qq_bot.chat.turn import _extra_notes


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


def test_no_notes_returns_none() -> None:
    assert _extra_notes("", []) is None


def test_duplicate_queries_merged() -> None:
    note = _extra_notes("", ["CS2 赛事", "CS2 赛事", "CS2 赛事（补查）"])
    assert note is not None
    assert note.count("CS2 赛事；") == 1
