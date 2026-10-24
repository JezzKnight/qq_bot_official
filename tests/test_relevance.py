"""关键词语境的规则快路与模型兜底（P2）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qq_bot.chat import relevance
from qq_bot.chat.relevance import KeywordKind


def test_find_keyword_exact_substring() -> None:
    assert relevance.find_keyword("大肥鱼 在吗", ["大肥鱼"]) == "大肥鱼"
    assert relevance.find_keyword("今天没看到它", ["大肥鱼"]) is None
    assert relevance.find_keyword("", ["大肥鱼"]) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("大肥鱼", KeywordKind.CALL),
        ("大肥鱼 在吗", KeywordKind.CALL),
        ("大肥鱼在不在", KeywordKind.CALL),
        ("大肥鱼帮我查下天气", KeywordKind.CALL),
        ("大肥鱼你在干嘛", KeywordKind.CALL),
        ("大肥鱼丸好吃吗", KeywordKind.HOMONYM),
        ("我家的那条大肥鱼又胖了", KeywordKind.HOMONYM),
    ],
)
def test_fast_kind_rules(text: str, expected: KeywordKind) -> None:
    assert relevance.fast_kind(text, "大肥鱼") is expected


def test_fast_kind_leaves_ambiguous_to_model() -> None:
    assert relevance.fast_kind("我觉得我的大肥鱼更牛逼", "大肥鱼") is None
    assert relevance.fast_kind("今天大肥鱼没说话", "大肥鱼") is None


@pytest.mark.asyncio
async def test_classify_uses_fast_path_without_network(monkeypatch) -> None:
    async def boom(_model: str) -> None:
        raise AssertionError

    monkeypatch.setattr(relevance, "get_client_for_model", boom)
    assert await relevance.classify("大肥鱼 在吗", "大肥鱼") is KeywordKind.CALL


@pytest.mark.asyncio
async def test_classify_fails_closed_to_homonym(monkeypatch) -> None:
    async def boom(_model: str) -> None:
        raise RuntimeError

    monkeypatch.setattr(relevance, "get_client_for_model", boom)
    kind = await relevance.classify("今天大肥鱼没说话", "大肥鱼")
    assert kind is KeywordKind.HOMONYM


@pytest.mark.asyncio
async def test_classify_parses_model_label(monkeypatch) -> None:
    class FakeClient:
        async def chat(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(content="about")

    async def fake_get_client(_model: str) -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(relevance, "get_client_for_model", fake_get_client)
    kind = await relevance.classify("今天大肥鱼没说话", "大肥鱼")
    assert kind is KeywordKind.ABOUT


@pytest.mark.asyncio
async def test_transition_cleans_quotes_and_prefix(monkeypatch) -> None:
    class FakeClient:
        async def chat(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(content="「回复：在说我啥呢？」")

    async def fake_get_client(_model: str) -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(relevance, "get_client_for_model", fake_get_client)
    text = await relevance.generate_transition(
        keyword="大肥鱼", text="我觉得我的大肥鱼更牛逼", nick="小明"
    )
    assert text == "在说我啥呢？"


@pytest.mark.asyncio
async def test_transition_returns_none_on_failure(monkeypatch) -> None:
    class FakeClient:
        async def chat(self, **_kwargs: object) -> None:
            raise TimeoutError

    async def fake_get_client(_model: str) -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(relevance, "get_client_for_model", fake_get_client)
    assert (
        await relevance.generate_transition(
            keyword="大肥鱼", text="今天大肥鱼没说话", nick="小明"
        )
        is None
    )
