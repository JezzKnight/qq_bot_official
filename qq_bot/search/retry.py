"""搜索结果不足时的判定与 query 改写。

第一次 fast 搜索失败，通常不是“网上没有”，而是 query 把实体/赛事/服务器
假设错了。这里只做两件事：判断回答是否明确表示“没查到”，以及让模型生成
几条不同角度的 query；真正的第二次搜索仍由 SearchPipeline 负责。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from qq_bot.ai.types import ChatMessage

if TYPE_CHECKING:
    from qq_bot.ai.openai_client import OpenaiCompatClient
    from qq_bot.search.types import UsageRecorder

logger = logging.getLogger(__name__)

RETRY_ACK = "这个我得好好查查"
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 120
DEFAULT_MAX_QUERIES = 3
MIN_RETRY_QUERIES = 2

# 这些词表示模型没有给出事实，而是明确说证据不足；宁可在边界上多补查一次，
# 也不要让“没查到”直接成为最终回复。
INSUFFICIENT_MARKERS = (
    "没查到",
    "没有查到",
    "未查到",
    "查不到",
    "没找到",
    "没有找到",
    "无法确认",
    "资料不足",
    "信息不足",
    "证据不足",
    "没有可靠",
    "缺乏可靠",
    "暂无",
    "尚未公布",
    "还没有公布",
)

_RETRY_PROMPT = """你是搜索 query 改写器。用户的问题在第一次搜索中没有找到可靠答案。

当前日期：{current_date}
用户问题：{question}

请生成 2-3 条不同的搜索 query，要求：
1. 保留人物、作品、赛事、地区、时间等核心实体，不要改变用户意图。
2. 对模糊词做展开：赛事全名/简称、日文名/英文名、服务器、年份月份、距离或项目。
3. 至少有一条用于确认“官方日程/官方公告”，避免只搜到旧攻略或旧新闻。
4. 不要简单重复原问题，也不要输出解释。
5. 只输出 JSON 字符串数组，例如 ["query 1", "query 2"]。

如果问题里出现“大赛”“活动”这类模糊词，要同时考虑它可能指不同赛事，
并把候选赛事名写进 query，而不是只加“最新”。"""


def looks_insufficient(text: str) -> bool:
    """回答是否明确表示没有查到可靠信息。"""
    normalized = text.strip()
    if not normalized:
        return True
    return any(marker in normalized for marker in INSUFFICIENT_MARKERS)


async def build_retry_queries(
    client: OpenaiCompatClient,
    model: str,
    question: str,
    *,
    max_queries: int = DEFAULT_MAX_QUERIES,
    usage_recorder: UsageRecorder | None = None,
) -> list[str]:
    """让轻量模型生成补查 query；失败时回退到通用变体，绝不阻断主流程。"""
    cleaned = " ".join(question.split()).strip()
    if not cleaned:
        return []
    if max_queries <= 1:
        return [cleaned[:MAX_QUERY_CHARS]]

    current_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    prompt = _RETRY_PROMPT.format(current_date=current_date, question=cleaned)
    try:
        response = await client.chat(
            messages=[ChatMessage(role="user", content=prompt)],
            model=model,
            temperature=0.2,
            max_tokens=1000,
        )
    except Exception:
        logger.warning("补查 query 改写失败，使用通用回退", exc_info=True)
        return _fallback_queries(cleaned, max_queries)

    if usage_recorder is not None:
        await usage_recorder(
            response.prompt_tokens,
            response.cached_tokens,
            response.completion_tokens,
        )

    queries = _parse_queries(response.content or "")
    if len(queries) < MIN_RETRY_QUERIES:
        logger.info("补查 query 改写结果过少，使用通用回退")
        queries = _fallback_queries(cleaned, max_queries)
    return queries[:max_queries]


def _fallback_queries(question: str, max_queries: int) -> list[str]:
    variants = [
        f"{question} 官方 最新 日程",
        f"{question} 详细 攻略 推荐",
        f"{question} 2026 最新",
    ]
    return [query[:MAX_QUERY_CHARS] for query in variants[:max_queries]]


def _parse_queries(content: str) -> list[str]:  # noqa: C901
    """兼容 JSON 数组、双重编码 JSON、代码块和逐行列表。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```").strip()

    data: object = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                data = None

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        data = data.get("queries")

    raw_queries: list[str]
    if isinstance(data, list):
        raw_queries = [str(item) for item in data]
    else:
        raw_queries = [
            line.strip().strip("-*0123456789.、 ")
            for line in text.splitlines()
            if line.strip()
        ]

    queries: list[str] = []
    for raw in raw_queries:
        query = " ".join(raw.split()).strip(" \t\"'`[]")
        if not (MIN_QUERY_CHARS <= len(query) <= MAX_QUERY_CHARS):
            continue
        if query in queries:
            continue
        queries.append(query)
    return queries
