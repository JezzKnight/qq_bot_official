"""记忆入口：持有 PgRepository 单例，并在启动时建池 + 跑迁移。

降级策略：初始化失败只记 ERROR 并返回 None，调用方按"无记忆对话"继续，
不阻断回复（设计 §10）。
"""

from __future__ import annotations

import logging

from qq_bot.memory.memory_config import get_memory_config
from qq_bot.memory.migrate import apply_migrations
from qq_bot.memory.pg_repo import PgRepository

logger = logging.getLogger(__name__)

class _State:
    """模块级单例状态（避免 global 语句）。"""

    repo: PgRepository | None = None


_state = _State()


async def init_memory() -> PgRepository | None:
    """建池 + 应用迁移；失败返回 None（后续每次调用会重试）。"""
    if _state.repo is not None:
        return _state.repo
    cfg = get_memory_config()
    if cfg.backend != "postgres":
        logger.error(
            "memory.yaml 的 backend=%s 已不再支持（回滚请使用 git revert）", cfg.backend
        )
        return None
    try:
        repo = await PgRepository.connect(cfg.connection_dsn())
        await apply_migrations(repo.pool)
    except Exception:
        logger.exception("记忆库初始化失败，将降级为无记忆对话")
        return None
    _state.repo = repo
    logger.info("记忆库已就绪（PostgreSQL）")
    return repo


async def get_memory() -> PgRepository | None:
    """取记忆仓库；首次调用时初始化。"""
    return await init_memory()


async def close_memory() -> None:
    if _state.repo is not None:
        await _state.repo.close()
        _state.repo = None
