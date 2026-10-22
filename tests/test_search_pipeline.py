from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from qq_bot.ai.types import ChatResponse
from qq_bot.search.cache import SearchCache
from qq_bot.search.deepseek import DeepSeekSearchProvider
from qq_bot.search.pipeline import SearchPipeline, resolve_mode
from qq_bot.search.planner import _parse_queries, infer_time_range
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
    from qq_bot.tools.tavily_client import TavilyClient


class FakeModel:
    def __init__(
        self, content: str = '["今日 英雄联盟 S赛 赛程", "S赛 2026 赛程"]'
    ) -> None:
        self.content = content

    async def chat(self, **_kwargs: object) -> ChatResponse:
        return ChatResponse(content=self.content)


class FakeTavily:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    async def search(self, payload: dict) -> dict:
        self.search_calls.append(payload)
        query = str(payload["query"])
        return {
            "results": [
                {
                    "title": f"{query} 结果 A",
                    "url": "https://example.com/a",
                    "content": "赛程 第一场 第二场",
                    "score": 0.9,
                    "published_date": "2026-10-20",
                },
                {
                    "title": f"{query} 结果 B",
                    "url": "https://example.org/b",
                    "content": "门票 场馆 交通",
                    "score": 0.8,
                },
            ]
        }

    async def extract(self, payload: dict) -> dict:
        self.extract_calls.append(payload)
        return {
            "results": [
                {
                    "url": url,
                    "raw_content": (
                        "<html><body><p>这是抓取到的正文，包含赛程和日期。</p>"
                        "<script>bad()</script></body></html>"
                    ),
                }
                for url in payload["urls"]
            ]
        }


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
    fake: FakeTavily,
    model: FakeModel | None = None,
    deepseek: FakeDeepSeek | None = None,
) -> SearchPipeline:
    return SearchPipeline(
        cast("TavilyClient", fake),
        cast("OpenaiCompatClient", model or FakeModel()),
        "fake-model",
        cache=SearchCache(ttl_seconds=60),
        fast_provider=cast("DeepSeekSearchProvider", deepseek),
    )


def test_resolve_mode_accepts_fast() -> None:
    assert resolve_mode("最近英雄联盟S赛赛程", "fast") == "fast"


def test_resolve_mode_auto_upgrades_deep_question() -> None:
    assert resolve_mode("最近英雄联盟S赛赛程", "auto") == "standard"
    assert resolve_mode("请详细分析这几个方案的优缺点并做对比", "auto") == "deep"
    assert resolve_mode("随便查查", "quick") == "quick"


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


def test_build_context_hard_caps_length() -> None:
    items = [
        SearchItem(f"标题{i}", f"https://example.com/{i}", "正文" * 400)
        for i in range(6)
    ]
    context = build_context(items, max_chars=600)
    assert len(context) <= 600


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


async def test_quick_mode_skips_reader() -> None:
    fake = FakeTavily()
    outcome = await _pipeline(fake).run("最近英雄联盟S赛赛程", mode="quick")
    assert outcome.error == ""
    assert outcome.items
    assert fake.extract_calls == []
    assert outcome.reader_sources == []
    assert "[S1]" in outcome.context


async def test_standard_mode_extracts_top_two_sources() -> None:
    fake = FakeTavily()
    outcome = await _pipeline(fake).run("最近英雄联盟S赛赛程", mode="standard")
    assert len(fake.search_calls) == 2
    assert len(fake.extract_calls) == 1
    assert len(fake.extract_calls[0]["urls"]) == 2
    assert any(item.source == "reader" for item in outcome.items)
    assert "<p>" not in outcome.context
    assert "这是抓取到的正文" in outcome.context


async def test_search_cache_avoids_duplicate_tavily_calls() -> None:
    fake = FakeTavily()
    pipeline = _pipeline(fake)
    first = await pipeline.run("最近英雄联盟S赛赛程", mode="standard")
    assert not first.cached
    calls_after_first = len(fake.search_calls)

    second = await pipeline.run("最近英雄联盟S赛赛程", mode="standard")
    assert second.cached
    assert len(fake.search_calls) == calls_after_first


def test_parse_queries_recovers_truncated_json_array() -> None:
    content = '["赛马娘 2026年11月 大赛 强势 马娘 推荐'
    assert _parse_queries(content) == ["赛马娘 2026年11月 大赛 强势 马娘 推荐"]


def test_parse_queries_handles_double_encoded_array() -> None:
    content = json.dumps(
        json.dumps(["赛马娘 2026年11月 大赛 推荐", "赛马娘 种马 技能"]),
        ensure_ascii=False,
    )
    assert _parse_queries(content) == [
        "赛马娘 2026年11月 大赛 推荐",
        "赛马娘 种马 技能",
    ]


def test_infer_time_range_for_recency_questions() -> None:
    assert infer_time_range("这几天什么地方的天气怎么样") == "week"
    assert infer_time_range("最近英雄联盟S赛赛程") == "month"
    assert infer_time_range("赛马娘有哪些马") is None


async def test_standard_search_requests_dates_and_time_range() -> None:
    fake = FakeTavily()
    await _pipeline(fake).run("最近英雄联盟S赛赛程", mode="standard")
    assert fake.search_calls
    assert all(call["include_published_date"] is True for call in fake.search_calls)
    assert all(call["time_range"] == "month" for call in fake.search_calls)


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


async def test_fast_mode_uses_deepseek_without_planner_or_reader() -> None:
    fake_tavily = FakeTavily()
    deepseek = FakeDeepSeek()
    outcome = await _pipeline(fake_tavily, deepseek=deepseek).run(
        "最近英雄联盟S赛赛程", mode="fast"
    )
    assert outcome.mode == "fast"
    assert outcome.error == ""
    assert outcome.items
    assert fake_tavily.search_calls == []
    assert fake_tavily.extract_calls == []
    assert len(deepseek.calls) == 1
    assert deepseek.calls[0]["need_content"] is False
    assert "[S1]" in outcome.context
    assert len(outcome.context) <= 4000


async def test_fast_mode_cache_avoids_duplicate_deepseek_calls() -> None:
    fake_tavily = FakeTavily()
    deepseek = FakeDeepSeek()
    pipeline = _pipeline(fake_tavily, deepseek=deepseek)
    first = await pipeline.run("最近英雄联盟S赛赛程", mode="fast")
    assert not first.cached
    second = await pipeline.run("最近英雄联盟S赛赛程", mode="fast")
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


async def test_fast_retry_rewrites_query_and_searches_each_one() -> None:
    fake_tavily = FakeTavily()
    deepseek = FakeDeepSeek()
    model = FakeModel(
        '["赛马娘 2026年11月 LoH 京都3000m 强势 马娘", '
        '"ウマ娘 2026年11月 リーグオブヒーローズ 京都3000m 攻略"]'
    )
    pipeline = _pipeline(fake_tavily, model, deepseek)

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
    assert fake_tavily.search_calls == []
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
