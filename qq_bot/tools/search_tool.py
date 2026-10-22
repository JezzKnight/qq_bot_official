"""主模型可调用的 `search` 工具：一次调用完成规划、搜索、重排、按需正文抓取。"""

from __future__ import annotations

import logging
import os

from nonebot import get_plugin_config

from qq_bot.ai.model_registry import get_registry
from qq_bot.search.deepseek import DeepSeekSearchProvider
from qq_bot.search.pipeline import SearchPipeline
from qq_bot.services import token_usage
from qq_bot.services.client_factory import (
    default_model_name,
    get_client_for_model,
)
from qq_bot.services.config import AiChatConfig
from qq_bot.tools.context import current_search_tracker
from qq_bot.tools.registry import register_tool
from qq_bot.tools.tavily_client import TavilyClient

logger = logging.getLogger(__name__)

_pipeline: SearchPipeline | None = None


async def _get_pipeline() -> SearchPipeline:
    """惰性构建：模块导入期没有 NoneBot driver，不能提前 get_plugin_config。"""
    global _pipeline  # noqa: PLW0603
    if _pipeline is None:
        config = get_plugin_config(AiChatConfig)
        model = default_model_name()
        client = await get_client_for_model(model)
        spec = get_registry().resolve(model)
        api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if not api_key and spec is not None and "deepseek" in spec.base_url:
            api_key = spec.api_key
        fast_provider = DeepSeekSearchProvider(api_key) if api_key else None
        _pipeline = SearchPipeline(
            TavilyClient(config),
            client,
            model,
            usage_recorder=token_usage.record,
            fast_provider=fast_provider,
        )
    return _pipeline


@register_tool(
    name="search",
    description=(
        "联网搜索：固定 fast 使用 DeepSeek 搜索 API，约 2-4 秒返回摘要。"
        "适合查新闻、赛程、活动、价格。"
        "游戏赛事/卡池先判断活动类型和服务器，不要把“大赛”默认成冠军会。"
        "游戏赛事第一次查只写“游戏名 服务器 年月 大赛 攻略”，不要预写冠军会。"
        "拿到结果后直接回答；只有结果不足时系统才换关键词补查一次。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "搜索关键词，不要只写整句问题。游戏赛事第一次查只写："
                    "游戏名 服务器 年月 大赛 攻略，不要预写冠军会/"
                    "チャンピオンズミーティング，先让搜索判断活动类型。"
                    "例：ウマ娘 日服 2026年11月 大赛 攻略"
                ),
            },
        },
        "required": ["query"],
    },
)
async def search(query: str, mode: str = "fast") -> str:
    tracker = current_search_tracker.get()
    if tracker is not None:
        tracker["search_rounds"] += 1

    try:
        pipeline = await _get_pipeline()
        outcome = await pipeline.run(
            query,
            mode=mode,
            force_refresh=False,
        )
    except Exception as exc:
        logger.exception("search 工具失败")
        if tracker is not None:
            tracker["tavily_error_count"] += 1
        return f"[搜索失败] {exc}"

    if tracker is not None:
        if outcome.items:
            tracker["tavily_success"] = True
        else:
            tracker["tavily_error_count"] += 1

    if not outcome.items:
        return f"[搜索失败] {outcome.error or '没有找到有效结果'}"
    if outcome.warnings:
        logger.info("search 警告: %s", "; ".join(outcome.warnings))
    return outcome.context


async def retry_search(query: str) -> str:
    """结果不足时由 turn 直接调用：改写 query 后再搜一轮，返回新的证据上下文。"""
    tracker = current_search_tracker.get()
    if tracker is not None:
        tracker["search_rounds"] += 1

    try:
        pipeline = await _get_pipeline()
        outcome = await pipeline.run_retry(query)
    except Exception:
        logger.exception("search 补查失败")
        if tracker is not None:
            tracker["tavily_error_count"] += 1
        return ""

    if tracker is not None:
        if outcome.items:
            tracker["tavily_success"] = True
        else:
            tracker["tavily_error_count"] += 1
    if not outcome.items:
        logger.info("search 补查无结果 query=%s error=%s", query, outcome.error)
        return ""
    if outcome.warnings:
        logger.info("search 补查警告: %s", "; ".join(outcome.warnings))
    return outcome.context


def reset_search_pipeline() -> None:
    """测试/基准脚本用：清空惰性单例。"""
    global _pipeline  # noqa: PLW0603
    _pipeline = None
