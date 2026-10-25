"""查询/攻略模式识别。"""

from __future__ import annotations

import pytest

from qq_bot.chat.query_mode import is_query_question


@pytest.mark.parametrize(
    "text",
    [
        "边狱巴士8-20贾丘怎么打",
        "这个boss怎么过",
        "求个配队",
        "天禄最近什么阵容",
        "这玩意在哪买",
        "多少钱",
        "这两个有什么区别",
        "推荐一下打野",
        "面板参数发我",
        "机制是啥",
    ],
)
def test_detects_query_questions(text: str) -> None:
    assert is_query_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "草 这不纯路边",
        "今天好热",
        "哈哈哈哈",
        "大肥鱼在不在",
        "你在干嘛",
        "晚安",
    ],
)
def test_ignores_casual_chat(text: str) -> None:
    assert not is_query_question(text)
