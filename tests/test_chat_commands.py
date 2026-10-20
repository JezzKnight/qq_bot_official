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


def test_model_keyboard_buttons() -> None:
    from qq_bot.platform.qqbot.model_panel import build_model_keyboard

    keyboard = build_model_keyboard(["deepseek-v4-flash", "glm-5.3"], "glm-5.3")
    rows = keyboard.content.rows
    assert rows is not None
    assert len(rows) == 2
    labels = [row.buttons[0].render_data.label for row in rows if row.buttons]
    assert labels == ["deepseek-v4-flash", "glm-5.3 ✓"]
    data = [row.buttons[0].action.data for row in rows if row.buttons]
    assert data == ["model:deepseek-v4-flash", "model:glm-5.3"]
