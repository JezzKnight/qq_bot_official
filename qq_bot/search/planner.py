"""查询规划：把口语化问题拆成 1-3 条互补的搜索 query。

只做一次轻量 LLM 调用，失败时退化为原问题；规划器不接触 Tavily。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from qq_bot.ai.types import ChatMessage

if TYPE_CHECKING:
    from qq_bot.ai.openai_client import OpenaiCompatClient
    from qq_bot.search.types import SearchMode

logger = logging.getLogger(__name__)

UsageRecorder = Callable[[int, int, int], Awaitable[None]]

_MAX_QUERY_CHARS = 120
_MIN_QUERY_CHARS = 2
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)、])\s*")
_PREFIX_RE = re.compile(r"^(?:query|搜索词|搜索|关键词)\s*[:：]\s*", re.IGNORECASE)


def _current_date_text() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return now.strftime("%Y年%m月%d日")


def _clean_query(raw: str) -> str:
    line = _BULLET_RE.sub("", raw.strip())
    line = _PREFIX_RE.sub("", line)
    line = line.strip()
    # 先剥 JSON 括号，再剥引号；截断输出常常只剩 "[ 或 "] 这种半边。
    for _ in range(2):
        line = line.strip("[],").strip()
        line = line.strip("\"'“”").strip()
    return line


def infer_time_range(question: str) -> str | None:
    """按问题里的时间词给出 Tavily 的时间窗，避免“最近”搜回半年前的页面。"""
    if any(
        marker in question
        for marker in (
            "这几天",
            "近日",
            "本周",
            "这周",
            "今日",
            "今天",
            "目前",
            "当前",
            "现在",
        )
    ):
        return "week"
    if any(
        marker in question
        for marker in (
            "最近",
            "最新",
            "新出",
            "本月",
            "这个月",
            "下个月",
            "下月",
            "今年",
            "下一次",
            "赛程",
            "卡池",
            "新活动",
        )
    ):
        return "month"
    return None


def _parse_queries(content: str) -> list[str]:  # noqa: C901 - 兼容多种模型输出格式
    """兼容 JSON 数组、{"queries": [...]} 和逐行列表三种模型输出。"""
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    candidates: list[str] = []
    if text.startswith(("[", "{", '"')):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, str):
            # 模型偶尔把整个数组再包一层 JSON 字符串；再解一次。
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = None
        if isinstance(data, list):
            candidates = [str(item) for item in data if isinstance(item, str)]
        elif isinstance(data, dict):
            raw_queries = data.get("queries")
            if isinstance(raw_queries, list):
                candidates = [
                    str(item) for item in raw_queries if isinstance(item, str)
                ]

    if not candidates:
        # JSON 被截断时，直接摘出所有引号内的 query，避免把 ["... 当成搜索词。
        quoted = re.findall(r'"([^"\n]{2,})"', text)
        candidates = quoted or [
            line
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith(("#", "//"))
        ]

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        query = _clean_query(raw)
        if not query or len(query) < _MIN_QUERY_CHARS or len(query) > _MAX_QUERY_CHARS:
            continue
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(query)
    return cleaned


async def plan_queries(  # noqa: PLR0913
    client: OpenaiCompatClient,
    model: str,
    question: str,
    mode: SearchMode,
    *,
    max_queries: int,
    usage_recorder: UsageRecorder | None = None,
) -> list[str]:
    """生成搜索词；任何异常都回退到原始问题，保证搜索链路可继续。"""
    if max_queries <= 1:
        return [question.strip()]

    system_prompt = (
        "你是搜索查询规划器。把用户问题改写成 1-3 条互补的搜索 query。\n"
        "要求：\n"
        "1. 保留关键实体；不要编造事实，不要回答问题。\n"
        "2. 用户说“最近/最新/今年/这个月”时，把当前年份写进 query。\n"
        "3. 需要对比或原因时，拆成对象 + 时间 + 指标几个 query。\n"
        '4. 只输出 JSON 字符串数组，例如 ["...", "..."]，不要解释。\n'
        f"今天是 {_current_date_text()}，模式是 {mode}。\n"
        f"最多输出 {max_queries} 条。"
    )
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=question),
    ]
    try:
        response = await client.chat(
            messages=messages,
            model=model,
            temperature=0.2,
            max_tokens=600,
        )
    except Exception as exc:  # noqa: BLE001 - 规划失败必须降级，不能阻断搜索
        logger.warning("搜索查询规划失败，回退原问题: %s", exc)
        return [question.strip()]

    if usage_recorder is not None:
        await usage_recorder(
            response.prompt_tokens,
            response.cached_tokens,
            response.completion_tokens,
        )

    queries = _parse_queries(response.content or "")
    if not queries:
        queries = [question.strip()]
    return queries[:max_queries]
