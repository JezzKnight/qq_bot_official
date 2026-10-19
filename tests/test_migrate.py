"""迁移器与 schema 的结构性验证。"""

from __future__ import annotations

from qq_bot.memory.migrate import apply_migrations

EXPECTED_TABLES = {
    "users",
    "group_members",
    "user_names",
    "threads",
    "messages",
    "group_summaries",
    "memories",
    "glossary_terms",
    "stickers",
    "identity_map",
    "schema_version",
}


async def test_migrations_create_all_tables(db) -> None:
    rows = await db.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    assert EXPECTED_TABLES <= {r["tablename"] for r in rows}


async def test_schema_version_is_one(db) -> None:
    assert await db.fetchval("SELECT max(version) FROM schema_version") == 8


async def test_migrations_idempotent(db) -> None:
    assert await apply_migrations(db) == 8


async def test_all_tables_and_columns_have_comments(db) -> None:
    """新增表/字段必须写注释（v004 之后的不变量）。"""
    missing_tables = await db.fetch(
        """
        SELECT c.relname FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND obj_description(c.oid) IS NULL
        """
    )
    missing_columns = await db.fetch(
        """
        SELECT c.relname, a.attname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND a.attnum > 0 AND NOT a.attisdropped
           AND col_description(c.oid, a.attnum) IS NULL
        """
    )
    assert missing_tables == [], [r["relname"] for r in missing_tables]
    assert missing_columns == [], [dict(r) for r in missing_columns]
