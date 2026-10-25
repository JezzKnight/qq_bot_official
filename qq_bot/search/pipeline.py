"""搜索管线：DeepSeek 搜索 API → 去重、重排 → 给模型的上下文。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from qq_bot.search.cache import SearchCache, make_cache_key
from qq_bot.search.rerank import build_context, dedupe_and_rerank, sort_by_recency
from qq_bot.search.retry import build_retry_queries
from qq_bot.search.types import SearchItem, SearchOutcome

if TYPE_CHECKING:
    from qq_bot.ai.openai_client import OpenaiCompatClient
    from qq_bot.search.deepseek import DeepSeekSearchProvider
    from qq_bot.search.types import UsageRecorder

logger = logging.getLogger(__name__)

_FAST_RESULTS = 10
_FAST_CONTEXT_CHARS = 5000
DEFAULT_RETRY_QUERIES = 3


class SearchPipeline:
    """单一路径：每次都走 DeepSeek 搜索 API，返回压缩后的摘要。"""

    def __init__(
        self,
        provider: DeepSeekSearchProvider,
        client: OpenaiCompatClient,
        model: str,
        *,
        cache: SearchCache | None = None,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._provider = provider
        self._client = client
        self._model = model
        self._cache = cache or SearchCache()
        self._usage_recorder = usage_recorder

    async def run(
        self,
        question: str,
        *,
        mode: str = "auto",  # noqa: ARG002 —— 保留工具 schema 的兼容参数
        force_refresh: bool = False,
        time_range: str | None = None,
    ) -> SearchOutcome:
        started = time.perf_counter()
        outcome = SearchOutcome(question=question, mode="fast")
        if not question.strip():
            outcome.error = "问题为空"
            return outcome
        return await self._run_queries(
            question,
            [question],
            outcome,
            time_range=time_range,
            force_refresh=force_refresh,
            started=started,
        )

    async def run_retry(
        self,
        question: str,
        *,
        max_queries: int = DEFAULT_RETRY_QUERIES,
        force_refresh: bool = False,
    ) -> SearchOutcome:
        """第一次回答“没查到”时，用改写后的 query 再搜一轮。"""
        started = time.perf_counter()
        outcome = SearchOutcome(question=question, mode="fast")
        if not question.strip():
            outcome.error = "问题为空"
            return outcome
        queries = await build_retry_queries(
            self._client,
            self._model,
            question,
            max_queries=max_queries,
            usage_recorder=self._usage_recorder,
        )
        if not queries:
            outcome.error = "没有生成补查 query"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        return await self._run_queries(
            question,
            queries,
            outcome,
            time_range=None,
            force_refresh=force_refresh,
            started=started,
        )

    async def _run_queries(  # noqa: PLR0913
        self,
        question: str,
        queries: list[str],
        outcome: SearchOutcome,
        *,
        time_range: str | None,
        force_refresh: bool,
        started: float,
    ) -> SearchOutcome:
        unique_queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
        if not unique_queries:
            outcome.error = "没有有效的搜索 query"
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        outcome.queries = unique_queries

        search_started = time.perf_counter()
        batches = await asyncio.gather(
            *(
                self._search(
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

    async def _search(
        self,
        query: str,
        *,
        question: str,
        time_range: str | None,
        force_refresh: bool,
    ) -> tuple[list[SearchItem], str, bool]:
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
            items = await self._provider.search(
                query,
                count=_FAST_RESULTS,
                need_content=False,
                ori_question=question,
            )
        except Exception as exc:  # noqa: BLE001 —— provider 失败要转成工具可读错误
            logger.warning("DeepSeek 搜索失败 query=%s: %s", query, exc)
            return [], f"DeepSeek 搜索失败: {query}", False
        if items:
            self._cache.put(key, items)
        return items, "", False
