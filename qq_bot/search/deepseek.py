"""DeepSeek 官方搜索 API provider。

只负责「一次查询 -> 规范化 SearchItem」，不做规划/重排/回答；
这些仍由 SearchPipeline 和上层模型完成，便于替换 provider 与单测。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from qq_bot.search.types import SearchItem

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1/tools/search"
DEFAULT_TIMEOUT = 30.0
MAX_RESULTS = 10
HTTP_OK = 200


class DeepSeekSearchError(RuntimeError):
    """DeepSeek 搜索请求失败（网络/鉴权/额度/响应格式）。"""


class DeepSeekSearchProvider:
    """DeepSeek 搜索 API 的薄适配器，默认只取 summary，避免正文挤爆上下文。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def search(
        self,
        query: str,
        *,
        count: int = 5,
        need_content: bool = False,
        ori_question: str = "",
    ) -> list[SearchItem]:
        """调用 DeepSeek Search；时效由搜索词中的时间表达承担。"""
        if not self._api_key:
            raise DeepSeekSearchError("DEEPSEEK_API_KEY 未配置")  # noqa: TRY003
        payload: dict[str, Any] = {
            "query": query.strip(),
            "count": max(1, min(int(count), MAX_RESULTS)),
            "need_content": need_content,
        }
        if ori_question.strip():
            payload["ori_question"] = ori_question.strip()

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._base_url,
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise DeepSeekSearchError(  # noqa: TRY003
                f"DeepSeek 搜索网络错误: {exc}"
            ) from exc

        if response.status_code != HTTP_OK:
            detail = response.text[:300].replace("\n", " ")
            raise DeepSeekSearchError(  # noqa: TRY003
                f"DeepSeek 搜索 HTTP {response.status_code}: {detail}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise DeepSeekSearchError("DeepSeek 搜索返回非 JSON") from exc  # noqa: TRY003
        if not isinstance(data, dict):
            raise DeepSeekSearchError("DeepSeek 搜索返回格式异常")  # noqa: TRY003
        return self._parse_results(data, need_content=need_content)

    @staticmethod
    def _parse_results(
        data: dict[str, Any],
        *,
        need_content: bool,
    ) -> list[SearchItem]:
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
            summary = str(raw.get("summary") or "")
            content = str(raw.get("content") or "")
            body = content if need_content and content else (summary or content)
            published_date = str(raw.get("date") or raw.get("published_date") or "")
            items.append(
                SearchItem(
                    title=title,
                    url=url,
                    content=body,
                    score=_float_or_zero(raw.get("rerank_score") or raw.get("score")),
                    published_date=published_date,
                    source=str(raw.get("site_name") or "deepseek"),
                )
            )
        return items


def _float_or_zero(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
