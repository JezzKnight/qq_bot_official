"""搜索管线：DeepSeek 搜索 + 重排 + 缓存 + 补查。"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from qq_bot.ai.types import ChatResponse
from qq_bot.search.cache import SearchCache
from qq_bot.search.deepseek import DeepSeekSearchProvider
from qq_bot.search.pipeline import SearchPipeline
from qq_bot.search.rerank import (
    build_context,
    clean_snippet,
    dedupe_and_rerank,
    published_timestamp,
    sort_by_recency,
)
from qq_bot.search.retry import RETRY_ACK, build_retry_queries, looks_insufficient
from qq_bot.search.types import SearchItem

if TYPE_CHECKING:
    from qq_bot.ai.openai_client import OpenaiCompatClient


class FakeModel:
    def __init__(
        self, content: str = '["今日 英雄联盟 S赛 赛程", "S赛 2026 赛程"]'
    ) -> None:
        self.content = content

    async def chat(self, **_kwargs: object) -> ChatResponse:
        return ChatResponse(content=self.content)


class FakeDeepSeek:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(
        self,
        query: str,
        *,
        count: int = 5,
        need_content: bool = False,
        ori_question: str = "",
        time_range: str | None = None,
    ) -> list[SearchItem]:
        self.calls.append(
            {
                "query": query,
                "count": count,
                "need_content": need_content,
                "ori_question": ori_question,
                "time_range": time_range,
            }
        )
        return [
            SearchItem(
                f"DeepSeek {index}",
                f"https://ds.example/{index}",
                "DeepSeek 摘要 包含赛程和日期",
                score=0.8,
                published_date="2026-10-21",
                source="deepseek",
            )
            for index in range(3)
        ]


def _pipeline(
    model: FakeModel | None = None,
    deepseek: FakeDeepSeek | None = None,
) -> SearchPipeline:
    return SearchPipeline(
        cast("DeepSeekSearchProvider", deepseek or FakeDeepSeek()),
        cast("OpenaiCompatClient", model or FakeModel()),
        "fake-model",
        cache=SearchCache(ttl_seconds=60),
    )


def test_clean_snippet_removes_tags_and_scripts() -> None:
    cleaned = clean_snippet("<p>你好</p><script>x()</script>&amp;世界")
    assert "你好" in cleaned
    assert "世界" in cleaned
    assert "<" not in cleaned
    assert "x()" not in cleaned


def test_dedupe_and_rerank_prefers_diverse_domains() -> None:
    items = [
        SearchItem("赛程 A", "https://a.com/1", "赛程"),
        SearchItem("赛程 A", "https://a.com/1", "重复"),
        SearchItem("赛程 B", "https://a.com/2", "赛程"),
        SearchItem("赛程 C", "https://b.com/3", "赛程"),
    ]
    ranked = dedupe_and_rerank(items, "赛程", limit=2)
    assert len(ranked) == 2
    assert len({item.url for item in ranked}) == 2
    assert len({item.url.split("/")[2] for item in ranked}) == 2


def test_build_context_numbers_sources() -> None:
    context = build_context(
        [
            SearchItem(
                "标题一", "https://a.com", "正文一", published_date="2026-10-20"
            ),
            SearchItem("标题二", "https://b.com", "正文二"),
        ]
    )
    assert "[S1]" in context
    assert "[S2]" in context
    assert "2026-10-20" in context


def test_published_timestamp_parses_rfc_and_iso_dates() -> None:
    assert published_timestamp("Tue, 20 Oct 2026 10:35:00 GMT") > 0
    assert published_timestamp("2026-10-20T10:35:00+08:00") > 0
    assert published_timestamp("") == 0


def test_sort_by_recency_prefers_newest_and_keeps_undated_last() -> None:
    items = [
        SearchItem("旧", "https://a.com/old", "旧", published_date="2026-09-01"),
        SearchItem("新", "https://b.com/new", "新", published_date="2026-10-20"),
        SearchItem("无日期", "https://c.com/none", "无日期"),
    ]
    ordered = sort_by_recency(items)
    assert [item.title for item in ordered] == ["新", "旧", "无日期"]


async def test_run_uses_deepseek_summary_only() -> None:
    deepseek = FakeDeepSeek()
    outcome = await _pipeline(deepseek=deepseek).run("最近英雄联盟S赛赛程")
    assert outcome.mode == "fast"
    assert outcome.error == ""
    assert outcome.items
    assert len(deepseek.calls) == 1
    assert deepseek.calls[0]["need_content"] is False
    assert "[S1]" in outcome.context
    assert len(outcome.context) <= 5000


async def test_cache_avoids_duplicate_deepseek_calls() -> None:
    deepseek = FakeDeepSeek()
    pipeline = _pipeline(deepseek=deepseek)
    first = await pipeline.run("最近英雄联盟S赛赛程")
    assert not first.cached
    second = await pipeline.run("最近英雄联盟S赛赛程")
    assert second.cached
    assert len(deepseek.calls) == 1


def test_deepseek_parse_uses_summary_by_default_and_content_when_requested() -> None:
    data = {
        "results": [
            {
                "title": "标题",
                "url": "https://example.com/a",
                "summary": "摘要",
                "content": "正文",
                "date": "2026-10-21",
                "rerank_score": 0.9,
                "site_name": "example",
            }
        ]
    }
    summary_item = DeepSeekSearchProvider._parse_results(data, need_content=False)[0]
    content_item = DeepSeekSearchProvider._parse_results(data, need_content=True)[0]
    assert summary_item.content == "摘要"
    assert content_item.content == "正文"
    assert summary_item.published_date == "2026-10-21"
    assert summary_item.score == 0.9
    assert summary_item.source == "example"


def test_looks_insufficient_only_matches_explicit_no_data_answers() -> None:
    assert RETRY_ACK == "这个我得好好查查"
    assert looks_insufficient("")
    assert looks_insufficient("没查到 2026 年 11 月的可靠资料")
    assert looks_insufficient("目前无法确认下个月的具体赛程")
    assert not looks_insufficient("决赛在 11 月 15 日 03:00 开始")
    assert not looks_insufficient("我不确定具体时间，但决赛在 11 月 15 日")


async def test_retry_rewrites_query_and_searches_each_one() -> None:
    deepseek = FakeDeepSeek()
    model = FakeModel(
        '["赛马娘 2026年11月 LoH 京都3000m 强势 马娘", '
        '"ウマ娘 2026年11月 リーグオブヒーローズ 京都3000m 攻略"]'
    )
    pipeline = _pipeline(model, deepseek)

    outcome = await pipeline.run_retry("下个月赛马娘大赛什么马比较强势")

    assert len(deepseek.calls) == 2
    assert outcome.queries == [
        "赛马娘 2026年11月 LoH 京都3000m 强势 马娘",
        "ウマ娘 2026年11月 リーグオブヒーローズ 京都3000m 攻略",
    ]
    assert all(
        call["ori_question"] == "下个月赛马娘大赛什么马比较强势"
        for call in deepseek.calls
    )
    assert outcome.error == ""
    assert "[S1]" in outcome.context


async def test_build_retry_queries_falls_back_when_model_returns_garbage() -> None:
    queries = await build_retry_queries(
        cast("OpenaiCompatClient", FakeModel("这不是 JSON")),
        "fake-model",
        "赛马娘大赛",
    )
    assert len(queries) >= 2
    assert all("赛马娘大赛" in query for query in queries)
