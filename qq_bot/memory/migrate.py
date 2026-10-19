"""schema 迁移：按 schema_version 增量应用 ``schema/v*.sql``。

设计要点：
- 每个版本文件在**一个事务**里执行，成功后写入版本号；失败则整版回滚。
- 幂等：已应用的版本直接跳过，可随每次启动调用。
- 不引入 alembic：本项目只有单向的初始建表与少量增量，轻量迁移器足够。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _migrations() -> list[tuple[int, Path]]:
    """按版本号升序返回 (version, path)。"""
    found: list[tuple[int, Path]] = []
    for path in SCHEMA_DIR.glob("v*.sql"):
        match = re.match(r"v(\d+)", path.name)
        if match:
            found.append((int(match.group(1)), path))
    return sorted(found)


async def apply_migrations(pool: asyncpg.Pool) -> int:
    """应用所有未执行的迁移，返回当前版本号。"""
    applied = 0
    async with pool.acquire() as conn:
        await conn.execute(_BOOTSTRAP)
        row = await conn.fetchrow("SELECT max(version) AS v FROM schema_version")
        current = int(row["v"] or 0)
        applied = current
        for version, path in _migrations():
            if version <= current:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_version(version) VALUES($1)", version
                )
            applied = version
    return applied
