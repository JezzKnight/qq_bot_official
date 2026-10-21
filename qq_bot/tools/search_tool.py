"""主模型可调用的 `search` 工具：一次调用完成规划、搜索、重排、按需正文抓取。"""

from __future__ import annotations

import logging

from nonebot import get_plugin_config

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
        _pipeline = SearchPipeline(
            TavilyClient(config),
            client,
            model,
            usage_recorder=token_usage.record,
        )
    return _pipeline


@register_tool(
    name="search",
    description=(
        "联网搜索：一次调用自动规划 query、搜索、去重重排，并按需抓取正文。"
        "适合查新闻、赛程、活动、价格、攻略等实时信息。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "用自然语言描述要查什么，尽量带时间、对象、地区",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "quick", "standard", "deep"],
                "description": (
                    "auto 默认；quick 只快查；deep 多 query + 正文，适合对比/分析"
                ),
            },
        },
        "required": ["query"],
    },
)
async def search(query: str, mode: str = "auto") -> str:
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


def reset_search_pipeline() -> None:
    """测试/基准脚本用：清空惰性单例。"""
    global _pipeline  # noqa: PLW0603
    _pipeline = None
