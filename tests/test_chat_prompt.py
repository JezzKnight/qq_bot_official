"""系统提示词拼接：顺序固定、空段跳过。"""

from __future__ import annotations

from qq_bot.chat.prompt import SystemPromptParts, build_system_prompt


def test_order_is_fixed() -> None:
    parts = SystemPromptParts(
        system="S",
        group="G",
        sticker="K",
        summary="U",
        speaker="P",
        state="X",
        memory="M",
        time="T",
    )
    assert build_system_prompt(parts) == (
        "S\n\nG\n\nK\n\nU\n\nP\n\nX\n\nM\n\nT"
    )


def test_empty_segments_are_skipped() -> None:
    parts = SystemPromptParts(
        system="S",
        group="",
        sticker="",
        summary="",
        speaker="P",
        state="",
        memory="",
        time="T",
    )
    assert build_system_prompt(parts) == "S\n\nP\n\nT"
