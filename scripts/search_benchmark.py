"""搜索双链路基准。

legacy = SearchAgent + batch_search + web_fetch；new = SearchPipeline。

用法：
    .venv/bin/python scripts/search_benchmark.py

结果写入 docs/search_benchmark_raw.json；脚本不修改生产配置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prompts.service import prompt_service
from qq_bot.agents.search_agent import SearchAgent
from qq_bot.ai.types import ChatMessage
from qq_bot.search.pipeline import SearchPipeline
from qq_bot.services.client_factory import (
    close_all,
    default_model_name,
    get_client_for_model,
)
from qq_bot.services.config import AiChatConfig
from qq_bot.tools import TOOLS, get_tools_schema, web_fetch, web_search
from qq_bot.tools.tavily_client import TavilyClient

if TYPE_CHECKING:
    from qq_bot.ai.openai_client import OpenaiCompatClient

ROOT = Path(__file__).resolve().parents[1]
RAW_PATH = ROOT / "docs" / "search_benchmark_raw.json"

logger = logging.getLogger("search_benchmark")

QUESTIONS: list[dict[str, str]] = [
    {"id": "lol_s", "category": "游戏", "text": "最近英雄联盟S赛赛程"},
    {"id": "anime_new", "category": "动漫", "text": "最近新出了什么动漫"},
    {
        "id": "uma_next",
        "category": "游戏",
        "text": "下个月赛马娘大赛什么马比较强势 什么技能要做进种马里",
    },
    {"id": "weather", "category": "现实", "text": "这几天什么地方的天气怎么样"},
    {
        "id": "uma_gacha",
        "category": "游戏",
        "text": "赛马娘日服最近有什么新卡池和新活动",
    },
    {
        "id": "oil_price",
        "category": "现实",
        "text": "2026年10月国内油价下一次调整是什么时候",
    },
]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt: int, cached: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.cached_tokens += cached
        self.completion_tokens += completion
        self.calls += 1

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


class UsageMeter:
    def __init__(self) -> None:
        self.usage = Usage()

    async def record(self, prompt: int, cached: int, completion: int) -> None:
        self.usage.add(prompt, cached, completion)

    def record_sync(self, prompt: int, cached: int, completion: int) -> None:
        self.usage.add(prompt, cached, completion)


async def synthesize(  # noqa: PLR0913, PLR0917
    client: OpenaiCompatClient,
    model: str,
    system_prompt: str,
    question: str,
    evidence: str,
    meter: UsageMeter,
) -> str:
    evidence_text = evidence.strip()[:14000]
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(
            role="user",
            content=f"问题：{question}\n\n证据：\n{evidence_text or '（无）'}",
        ),
    ]

    async def _call(limit: int) -> str:
        response = await client.chat(
            messages=messages,
            model=model,
            temperature=0.2,
            max_tokens=limit,
        )
        meter.record_sync(
            response.prompt_tokens,
            response.cached_tokens,
            response.completion_tokens,
        )
        return (response.content or "").strip()

    answer = await _call(1600)
    if not answer:
        # reasoning 模型有时把 token 全花在思考上，内容为空；提高上限重试一次。
        answer = await _call(2400)
    return answer


async def run_legacy(
    question: str,
    client: OpenaiCompatClient,
    model: str,
    system_prompt: str,
) -> dict[str, object]:
    meter = UsageMeter()
    started = time.perf_counter()
    error = ""
    report = ""
    try:
        agent = SearchAgent(
            client,
            get_tools_schema("batch_search", "web_fetch"),
            model,
            question,
            TOOLS,
            meter.record,
        )
        report = await agent.execute("搜索失败，请说明没有查到可靠信息")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("legacy 检索失败")
    retrieval_s = round(time.perf_counter() - started, 2)

    answer = ""
    if not error:
        try:
            answer = await synthesize(
                client, model, system_prompt, question, report, meter
            )
        except Exception as exc:
            error = f"answer: {type(exc).__name__}: {exc}"
            logger.exception("legacy 回答失败")

    total_s = round(time.perf_counter() - started, 2)
    return {
        "answer": answer,
        "report": report[:4000],
        "error": error,
        "retrieval_s": retrieval_s,
        "total_s": total_s,
        "usage": meter.usage.to_dict(),
        "source_count": report.count("http"),
    }


async def run_improved(
    question: str,
    client: OpenaiCompatClient,
    model: str,
    system_prompt: str,
    tavily: TavilyClient,
) -> dict[str, object]:
    meter = UsageMeter()
    started = time.perf_counter()
    error = ""
    outcome = None
    try:
        pipeline = SearchPipeline(
            tavily,
            client,
            model,
            usage_recorder=meter.record,
        )
        outcome = await pipeline.run(question, force_refresh=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("new 检索失败")
    retrieval_s = round(time.perf_counter() - started, 2)

    answer = ""
    if outcome is not None and not error:
        try:
            answer = await synthesize(
                client,
                model,
                system_prompt,
                question,
                outcome.context,
                meter,
            )
        except Exception as exc:
            error = f"answer: {type(exc).__name__}: {exc}"
            logger.exception("new 回答失败")

    total_s = round(time.perf_counter() - started, 2)
    return {
        "answer": answer,
        "error": error,
        "retrieval_s": retrieval_s,
        "total_s": total_s,
        "usage": meter.usage.to_dict(),
        "mode": outcome.mode if outcome else "",
        "queries": outcome.queries if outcome else [],
        "warnings": outcome.warnings if outcome else [],
        "search_ms": outcome.search_ms if outcome else 0,
        "reader_ms": outcome.reader_ms if outcome else 0,
        "source_count": outcome.source_count if outcome else 0,
        "sources": [
            {
                "title": item.title,
                "url": item.url,
                "published_date": item.published_date,
                "source": item.source,
            }
            for item in (outcome.items if outcome else [])
        ],
    }


async def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logger.setLevel(logging.INFO)
    env = dotenv_values(ROOT / ".env")
    tavily_raw = env.get("TAVILY_KEY") or ""
    config = AiChatConfig(Tavily_key=tavily_raw)
    for key in ("DEEPSEEK_API_KEY", "GLM_API_KEY"):
        value = env.get(key)
        if value:
            os.environ.setdefault(key, value)
    if not config.Tavily_key:
        raise SystemExit("TAVILY_KEY 未配置")  # noqa: TRY003

    model = default_model_name()
    client = await get_client_for_model(model)
    tavily = TavilyClient(config)
    # 让 legacy 的 batch_search / web_fetch 使用同一份配置，避免依赖 NoneBot driver。
    web_search._client = tavily
    web_fetch._client = tavily

    current_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    system_prompt = prompt_service.get_agent_prompt(
        "search_answer", current_date=current_date
    )
    results: list[dict[str, object]] = []
    started_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

    try:
        for index, question in enumerate(QUESTIONS, start=1):
            text = question["text"]
            logger.info("[%d/%d] %s", index, len(QUESTIONS), text)

            legacy = await run_legacy(text, client, model, system_prompt)
            await asyncio.sleep(1)
            improved = await run_improved(text, client, model, system_prompt, tavily)
            await asyncio.sleep(1)

            item = {
                **question,
                "legacy": legacy,
                "improved": improved,
            }
            results.append(item)
            RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
            RAW_PATH.write_text(
                json.dumps(
                    {
                        "generated_at": started_at,
                        "model": model,
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            legacy_usage = legacy["usage"]
            new_usage = improved["usage"]
            logger.info(
                "  legacy %ss / %s tok | new %ss / %s tok",
                legacy["total_s"],
                legacy_usage["total_tokens"] if isinstance(legacy_usage, dict) else "?",
                improved["total_s"],
                new_usage["total_tokens"] if isinstance(new_usage, dict) else "?",
            )
    finally:
        await close_all()

    logger.info("已写入 %s", RAW_PATH)
    logger.info("完成 %d 个问题", len(results))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
