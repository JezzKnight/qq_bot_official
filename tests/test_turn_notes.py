"""系统台账兜底：模型若复述台账，发送前要剥掉。"""

from __future__ import annotations

from qq_bot.chat.turn import _strip_echoed_notes


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
