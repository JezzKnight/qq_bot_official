"""指令解析：/name args。"""

from __future__ import annotations

from qq_bot.chat.commands import parse_command


def test_parse_with_args() -> None:
    assert parse_command("/usage 2026-10-18") == ("usage", "2026-10-18")


def test_parse_without_args() -> None:
    assert parse_command("/reset") == ("reset", "")


def test_parse_chinese_alias() -> None:
    assert parse_command("/用量") == ("用量", "")


def test_non_command_returns_empty() -> None:
    assert parse_command("你好") == ("", "")
    assert parse_command("/") == ("", "")
