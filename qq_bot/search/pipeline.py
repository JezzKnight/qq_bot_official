"""改进后的搜索管线：规划 → 多 query 搜索 → 去重重排 → 按需 reader → 上下文。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from qq_bot.search.cache import SearchCache, make_cache_key
from qq_bot.search.planner import UsageRecorder, infer_time_range, plan_queries
from qq_bot.search.rerank import (
    build_context,
    clean_snippet,
    dedupe_and_rerank,
    normalize_url,
    sort_by_recency,
)
from qq_bot.search.retry import build_retry_queries
from qq_bot.search.types import SearchItem, SearchMode, SearchOutcome

if TYPE_CHECKING:
    from qq_bot.ai.openai_client import OpenaiCompatClient
    from qq_bot.search.deepseek import DeepSeekSearchProvider
    from qq_bot.tools.tavily_client import TavilyClient

logger = logging.getLogger(__name__)

_DEEP_QUESTION_CHARS = 70

_DEEP_MARKERS = (
    "对比",
    "比较",
    "分析",
    "为什么",
    "详细",
    "全面",
    "排名",
    "推荐",
    "如何",
    "趋势",
    "影响",
)

_MODE_QUERY_LIMIT: dict[SearchMode, int] = {
    "fast": 1,
    "quick": 1,
    "standard": 2,
    "deep": 3,
}
_MODE_RESULTS: dict[SearchMode, int] = {
    "fast": 5,
    "quick": 4,
    "standard": 8,
    "deep": 10,
}
_MODE_READER_TOP_N: dict[SearchMode, int] = {
    "fast": 0,
    "quick": 0,
    "standard": 2,
    "deep": 3,
}
_MODE_DEPTH: dict[SearchMode, str] = {
    "fast": "basic",
    "quick": "basic",
    "standard": "basic",
    "deep": "advanced",
}

_FAST_RESULTS = 5
_FAST_CONTEXT_CHARS = 3000


def resolve_mode(question: str, requested: str) -> SearchMode:
    """auto 只在问题明显需要多步/对比时才升级到 deep，其余走 standard。"""
    if requested in _MODE_QUERY_LIMIT:
        return cast("SearchMode", requested)
    if len(question) >= _DEEP_QUESTION_CHARS or any(
        marker in question for marker in _DEEP_MARKERS
    ):
        return "deep"
    return "standard"


def _float_or_zero(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class SearchPipeline:
    """Tavily 负责 standard/deep，DeepSeek Search 负责 fast；接口保持纯异步。"""

    def __init__(  # noqa: PLR0913
        self,
        tavily: TavilyClient,
        client: OpenaiCompatClient,
        model: str,
        *,
        cache: SearchCache | None = None,
        usage_recorder: UsageRecorder | None = None,
        fast_provider: DeepSeekSearchProvider | None = None,
    ) -> None:
        self._tavily = tavily
        self._fast_provider = fast_provider
        self._client = client
        self._model = model
        self._cache = cache or SearchCache()
        self._usage_recorder = usage_recorder

    async def run(  # noqa: C901
        self,
        question: str,
        *,
        mode: str = "auto",
        force_refresh: bool = False,
        time_range: str | None = None,
    ) -> SearchOutcome:
        started = time.perf_counter()
        resolved = resolve_mode(question, mode)
        outcome = SearchOutcome(question=question, mode=resolved)
        if not question.strip():
            outcome.error = "问题为空"
            return outcome
        if time_range is None:
            time_range = infer_time_range(question)
        if resolved == "fast":
            return await self._run_fast(
                question,
                outcome,
                time_range=time_range,
                force_refresh=force_refresh,
                started=started,
            )

        queries = await plan_queries(
            self._client,
            self._model,
            question,
            resolved,
            max_queries=_MODE_QUERY_LIMIT[resolved],
            usage_recorder=self._usage_recorder,
        )
        outcome.queries = queries
        depth = _MODE_DEPTH[resolved]
        max_results = _MODE_RESULTS[resolved]

        search_started = time.perf_counter()
        batches = await asyncio.gather(
            *(
                self._search_one(
                    query,
                    depth=depth,
                    max_results=max_results,
                    time_range=time_range,
                    force_refresh=force_refresh,
                )
                for query in queries
            )
        )
        outcome.search_ms = int((time.perf_counter() - search_started) * 1000)

        items: list[SearchItem] = []
        cache_hits = 0
        for batch, warning, from_cache in batches:
            items.extend(batch)
            if warning:
                outcome.warnings.append(warning)
            if from_cache:
                cache_hits += 1

        if not items:
            outcome.error = "没有搜索到结果"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        ranked = dedupe_and_rerank(items, question, limit=max_results)
        reader_top_n = _MODE_READER_TOP_N[resolved]
        if reader_top_n > 0 and ranked:
            reader_started = time.perf_counter()
            raw_pages, sources, reader_warning = await self._extract_top(
                question,
                ranked[:reader_top_n],
                depth="advanced" if resolved == "deep" else "basic",
            )
            outcome.reader_ms = int((time.perf_counter() - reader_started) * 1000)
            outcome.reader_sources = sources
            if reader_warning:
                outcome.warnings.append(reader_warning)
            ranked = self._apply_reader(ranked, raw_pages)

        if time_range:
            # 时效问题的证据按“新到旧”排，避免模型先看到半年前的旧页面。
            ranked = sort_by_recency(ranked)
        outcome.items = ranked
        outcome.context = build_context(ranked)
        outcome.cached = cache_hits == len(queries)
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    async def run_retry(
        self,
        question: str,
        *,
        max_queries: int = 3,
        force_refresh: bool = False,
    ) -> SearchOutcome:
        """第一次 fast 回答“没查到”时，用改写后的 query 再搜一轮。"""
        started = time.perf_counter()
        outcome = SearchOutcome(question=question, mode="fast")
        if not question.strip():
            outcome.error = "问题为空"
            return outcome
        if self._fast_provider is None:
            outcome.error = "DeepSeek 搜索未配置"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        queries = await build_retry_queries(
            self._client,
            self._model,
            question,
            max_queries=max_queries,
            usage_recorder=self._usage_recorder,
        )
        outcome.queries = queries
        if not queries:
            outcome.error = "没有生成补查 query"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        return await self._run_fast_queries(
            question,
            queries,
            outcome,
            time_range=infer_time_range(question),
            force_refresh=force_refresh,
            started=started,
        )

    async def _run_fast(
        self,
        question: str,
        outcome: SearchOutcome,
        *,
        time_range: str | None,
        force_refresh: bool,
        started: float,
    ) -> SearchOutcome:
        """DeepSeek Search 快路径：不规划、不抓正文，只保留压缩后的摘要。"""
        outcome.queries = [question]
        return await self._run_fast_queries(
            question,
            [question],
            outcome,
            time_range=time_range,
            force_refresh=force_refresh,
            started=started,
        )

    async def _run_fast_queries(  # noqa: PLR0913
        self,
        question: str,
        queries: list[str],
        outcome: SearchOutcome,
        *,
        time_range: str | None,
        force_refresh: bool,
        started: float,
    ) -> SearchOutcome:
        if self._fast_provider is None:
            outcome.error = "DeepSeek 搜索未配置"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        unique_queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
        if not unique_queries:
            outcome.error = "没有有效的搜索 query"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        outcome.queries = unique_queries

        search_started = time.perf_counter()
        batches = await asyncio.gather(
            *(
                self._fast_search(
                    query,
                    question=question,
                    time_range=time_range,
                    force_refresh=force_refresh,
                )
                for query in unique_queries
            )
        )
        outcome.search_ms = int((time.perf_counter() - search_started) * 1000)

        items: list[SearchItem] = []
        cache_hits = 0
        for batch, warning, from_cache in batches:
            items.extend(batch)
            if warning:
                outcome.warnings.append(warning)
            if from_cache:
                cache_hits += 1

        if not items:
            outcome.error = "DeepSeek 没有搜索到结果"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        ranked = dedupe_and_rerank(items, question, limit=_FAST_RESULTS)
        if time_range:
            ranked = sort_by_recency(ranked)
        outcome.items = ranked
        outcome.context = build_context(ranked, max_chars=_FAST_CONTEXT_CHARS)
        outcome.cached = cache_hits == len(unique_queries)
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    async def _fast_search(
        self,
        query: str,
        *,
        question: str,
        time_range: str | None,
        force_refresh: bool,
    ) -> tuple[list[SearchItem], str, bool]:
        provider = self._fast_provider
        if provider is None:
            return [], "DeepSeek 搜索未配置", False

        key = make_cache_key(
            query,
            depth="deepseek",
            max_results=_FAST_RESULTS,
            time_range=time_range,
        )
        if not force_refresh:
            cached = self._cache.get(key)
            if cached is not None:
                return cached, "", True

        try:
            items = await provider.search(
                query,
                count=_FAST_RESULTS,
                need_content=False,
                ori_question=question,
            )
        except Exception as exc:  # noqa: BLE001 - provider 失败要转成工具可读错误
            logger.warning("DeepSeek 搜索失败 query=%s: %s", query, exc)
            return [], f"DeepSeek 搜索失败: {query}", False
        if items:
            self._cache.put(key, items)
        return items, "", False

    async def _search_one(
        self,
        query: str,
        *,
        depth: str,
        max_results: int,
        time_range: str | None,
        force_refresh: bool,
    ) -> tuple[list[SearchItem], str, bool]:
        key = make_cache_key(
            query,
            depth=depth,
            max_results=max_results,
            time_range=time_range,
        )
        if not force_refresh:
            cached = self._cache.get(key)
            if cached is not None:
                return cached, "", True

        payload: dict[str, object] = {
            "query": query,
            "search_depth": depth,
            "topic": "general",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_published_date": True,
        }
        if time_range:
            payload["time_range"] = time_range

        try:
            data = await self._tavily.search(payload)
        except Exception as exc:  # noqa: BLE001 - 单条 query 失败不能拖垮整次搜索
            logger.warning("Tavily 搜索失败 query=%s: %s", query, exc)
            return [], f"搜索失败: {query}", False

        items = self._parse_results(data)
        if items:
            self._cache.put(key, items)
        return items, "", False

    @staticmethod
    def _parse_results(data: dict) -> list[SearchItem]:
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return []
        items: list[SearchItem] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            if not url:
                continue
            title = str(raw.get("title") or url).strip()
            content = str(raw.get("content") or raw.get("raw_content") or "")
            items.append(
                SearchItem(
                    title=title,
                    url=url,
                    content=content,
                    score=_float_or_zero(raw.get("score")),
                    published_date=str(raw.get("published_date") or ""),
                )
            )
        return items

    async def _extract_top(
        self,
        question: str,
        items: list[SearchItem],
        *,
        depth: str,
    ) -> tuple[dict[str, str], list[str], str]:
        payload = {
            "urls": [item.url for item in items],
            "extract_depth": depth,
            "format": "text",
            "query": question,
            "chunks_per_source": 3,
            "include_images": False,
        }
        try:
            data = await self._tavily.extract(payload)
        except Exception as exc:  # noqa: BLE001 - reader 失败降级为摘要
            logger.warning("Tavily extract 失败: %s", exc)
            return {}, [], "正文抓取失败，已回退摘要"

        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return {}, [], "正文抓取为空，已回退摘要"

        pages: dict[str, str] = {}
        sources: list[str] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            content = str(raw.get("raw_content") or raw.get("content") or "")
            if url and content:
                pages[normalize_url(url)] = content
                sources.append(url)
        return pages, sources, ""

    @staticmethod
    def _apply_reader(
        items: list[SearchItem],
        pages: dict[str, str],
    ) -> list[SearchItem]:
        if not pages:
            return items
        enriched: list[SearchItem] = []
        for item in items:
            raw = pages.get(normalize_url(item.url))
            if not raw:
                enriched.append(item)
                continue
            cleaned = clean_snippet(raw, max_chars=2200)
            if len(cleaned) > len(item.content):
                enriched.append(replace(item, content=cleaned, source="reader"))
            else:
                enriched.append(item)
        return enriched
