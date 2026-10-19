"""记忆清理：按分层 TTL 删除过期数据（设计 §9）。

- 原文（messages）：默认保留 30 天；
- 摘要（group_summaries）：默认保留 365 天，每组保留最新一条；
- 长期记忆（memories）：永不删除，只按 last_confirmed_at 停止注入。
"""

from __future__ import annotations

import logging

from nonebot_plugin_apscheduler import scheduler

from qq_bot.memory.memory_config import get_memory_config

from .memory_writing import get_memory

logger = logging.getLogger(__name__)

_JOB_ID = "memory_cleanup"


async def purge_expired() -> None:
    repo = await get_memory()
    if repo is None:
        return
    cfg = get_memory_config()
    try:
        messages = await repo.purge_messages(cfg.retention.raw_days)
        summaries = await repo.purge_summaries(cfg.retention.summary_days)
    except Exception:
        logger.exception("记忆清理任务失败")
        return
    logger.info(
        "记忆清理完成：messages=%s 条（>%s 天），summaries=%s 条（>%s 天）",
        messages,
        cfg.retention.raw_days,
        summaries,
        cfg.retention.summary_days,
    )


def schedule_cleanup() -> None:
    """每天 04:10 执行一次（replace_existing 保证重启后不重复注册）。"""
    scheduler.add_job(
        purge_expired,
        "cron",
        hour=4,
        minute=10,
        id=_JOB_ID,
        replace_existing=True,
    )
