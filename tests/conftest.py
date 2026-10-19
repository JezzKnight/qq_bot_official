"""pytest 夹具：真实 PostgreSQL 的隔离测试库（qq_bot_test）。

与生产库 qq_bot 完全隔离：session 级重建库并跑迁移，function 级清表。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import asyncpg
import pytest_asyncio
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

TEST_DB = "qq_bot_test"


def _dsn(database: str) -> str:
    user = os.getenv("PG_USER", "postgres")
    password = quote(os.getenv("PG_PASSWORD", ""), safe="")
    host = os.getenv("PG_HOST", "127.0.0.1")
    port = os.getenv("PG_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def _recreate_database() -> None:
    admin = await asyncpg.connect(_dsn("postgres"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await admin.close()


@pytest_asyncio.fixture(scope="session")
async def pg_pool():
    await _recreate_database()
    pool = await asyncpg.create_pool(_dsn(TEST_DB), min_size=1, max_size=4)
    from qq_bot.memory.migrate import apply_migrations

    await apply_migrations(pool)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def db(pg_pool):
    """函数级隔离：用完清空所有业务表（保留 schema 与版本）。"""
    yield pg_pool
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE messages, threads, user_names, group_members, users, "
            "group_summaries, memories, glossary_terms, stickers, identity_map "
            "RESTART IDENTITY CASCADE"
        )


@pytest_asyncio.fixture
async def repo(pg_pool):
    """PgRepository 实例；用完清空业务表。"""
    from qq_bot.memory.pg_repo import PgRepository

    yield PgRepository(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE messages, threads, user_names, group_members, users, "
            "group_summaries, memories, glossary_terms, stickers, identity_map "
            "RESTART IDENTITY CASCADE"
        )
