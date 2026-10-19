"""群摘要压缩的插件层：模型调用、异步调度、启动补扫。

存储逻辑在 qq_bot/memory/compress.py；这里只负责"用哪个模型、什么时候跑、失败怎么办"。
"""

from __future__ import annotations

import asyncio
import logging

from prompts.service import prompt_service
from qq_bot.ai.types import ChatMessage
from qq_bot.memory.compress import (
    CompressOutcome,
    compress_if_needed,
    format_transcript,
)
from qq_bot.memory.memory_config import get_memory_config

from .client_factory import get_client_for_model
from .memory_writing import get_memory

logger = logging.getLogger(__name__)

# 同群去重 + 持有任务引用（否则 create_task 的返回值没人引用会被 GC 警告）
_inflight: set[int] = set()
_tasks: set[asyncio.Task] = set()


async def summarize_with_model(
    previous: str | None, messages: list[dict], max_tokens: int
) -> str:
    """用低成本模型生成摘要；失败由上层捕获，不写库。"""
    cfg = get_memory_config()
    client = await get_client_for_model(cfg.compression.model)
    system_prompt = prompt_service.get_agent_prompt(
        "memory_summary",
        previous=previous or "（无）",
        transcript=format_transcript(messages),
        max_chars=str(max_tokens * 2),
    )
    response = await client.chat(
        messages=[
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content="请输出合并后的摘要正文。"),
        ],
        model=cfg.compression.model,
        temperature=0.3,
        max_tokens=max_tokens,
    )
    return (response.content or "").strip()


async def compress_group(group_id: int) -> CompressOutcome | None:
    repo = await get_memory()
    if repo is None:
        return None
    return await compress_if_needed(
        repo,
        group_id=group_id,
        config=get_memory_config(),
        summarize=summarize_with_model,
    )


def schedule_compress(group_id: int) -> None:
    """落库后异步触发；同群已有任务在跑则跳过。"""
    if group_id in _inflight:
        return
    _inflight.add(group_id)
    task = asyncio.create_task(_run(group_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run(group_id: int) -> None:
    try:
        outcome = await compress_group(group_id)
        if outcome is not None:
            logger.info(
                "群摘要已更新 group=%s 覆盖 %s..%s（%s 条 / %s tokens / 重建=%s）",
                group_id,
                outcome.covers_from,
                outcome.covers_to,
                outcome.messages,
                outcome.tokens,
                outcome.rebuilt,
            )
    except Exception:
        logger.exception("群摘要压缩失败 group=%s", group_id)
    finally:
        _inflight.discard(group_id)


async def sweep_active_groups() -> None:
    """启动补扫：把停机期间积压的群各自排一次压缩（不阻塞启动）。"""
    repo = await get_memory()
    if repo is None:
        return
    groups = await repo.active_group_ids(days=7)
    for group_id in groups:
        schedule_compress(group_id)
    if groups:
        logger.info("启动补扫：%d 个活跃群已排入摘要检查", len(groups))


def schedule_sweep() -> None:
    """在 startup 里 fire-and-forget，失败只记日志。"""
    task = asyncio.create_task(sweep_active_groups())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def shutdown() -> None:
    """取消尚未完成的压缩任务。

    否则连接池关闭后这些任务还会访问 PG，日志里会出现
    ``asyncpg.InterfaceError: pool is closing`` 的噪音错误。
    """
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _inflight.clear()
