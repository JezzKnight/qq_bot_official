"""窗口内三态判定：解析、上下文格式化、失败兜底。"""

from __future__ import annotations

import pytest

from qq_bot.chat import judge


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("REPLY", judge.JudgeAction.REPLY),
        ("reply\n", judge.JudgeAction.REPLY),
        ("PASS", judge.JudgeAction.PASS),
        ("SLEEP", judge.JudgeAction.SLEEP),
        ("SLEEP（他们在聊别的）", judge.JudgeAction.SLEEP),
        ("", judge.JudgeAction.PASS),
        ("不知道", judge.JudgeAction.PASS),
    ],
)
def test_parse_action(raw: str, expected: judge.JudgeAction) -> None:
    assert judge.parse_action(raw) is expected


def test_format_recent_marks_bot_and_names() -> None:
    text = judge.format_recent(
        [
            {"role": "user", "name": "小明", "content": "在吗"},
            {"role": "assistant", "name": "", "content": "在"},
        ]
    )
    assert text == "小明: 在吗\nbot: 在"


def test_format_recent_skips_empty() -> None:
    assert judge.format_recent([{"role": "user", "name": "小明", "content": " "}]) == ""


@pytest.mark.asyncio
async def test_judge_failure_falls_back_to_pass(monkeypatch) -> None:
    def boom(_model: str) -> None:
        raise RuntimeError

    monkeypatch.setattr(judge, "get_client_for_model", boom)
    action = await judge.judge_in_window(text="在吗", speaker="小明")
    assert action is judge.JudgeAction.PASS
