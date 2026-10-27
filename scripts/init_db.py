"""一次性初始化 PostgreSQL：建库；建表由 bot 启动时的迁移自动完成。

用法：
    uv run python scripts/init_db.py                    # 按 .env 的 PG_* 建库
    uv run python scripts/init_db.py --create-user      # 角色不存在时一并创建
    uv run python scripts/init_db.py --database qq_bot  # 覆盖库名

只做两件事：
1. 连到维护库 ``postgres``，检查目标库是否存在，不存在就 ``CREATE DATABASE``；
2. 打印下一步。表结构不在这里建——``apply_migrations()`` 会在 bot 第一次连库时
   按 ``qq_bot/memory/schema/v*.sql`` 增量建表，重复启动是幂等的。

脚本可重复执行：库已存在时只打印状态，不做任何修改。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import quote as urlquote

import asyncpg
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")

_DEFAULT_DB = "qq_bot_official"
_MAINTENANCE_DB = "postgres"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _dsn(database: str) -> str:
    user = _env("PG_USER", "postgres")
    password = urlquote(_env("PG_PASSWORD"), safe="")
    host = _env("PG_HOST", "127.0.0.1")
    port = _env("PG_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _ident(name: str) -> str:
    """只允许普通标识符，避免把用户输入拼进 DDL 时被注入。"""
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        msg = f"非法标识符: {name!r}"
        raise ValueError(msg)
    if name[0].isdigit():
        msg = f"标识符不能以数字开头: {name!r}"
        raise ValueError(msg)
    return f'"{name}"'


async def init(database: str, *, create_user: bool) -> int:
    user = _env("PG_USER", "postgres")
    try:
        conn = await asyncpg.connect(_dsn(_MAINTENANCE_DB))
    except Exception as exc:
        print(f"连不上 PostgreSQL（{_MAINTENANCE_DB} 库）: {exc}", file=sys.stderr)
        print(
            "检查 .env 的 PG_HOST/PG_PORT/PG_USER/PG_PASSWORD，"
            "以及 docker 容器是否把端口发布到 127.0.0.1:5432。",
            file=sys.stderr,
        )
        return 1

    try:
        role_exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", user
        )
        if not role_exists:
            if not create_user:
                print(
                    f"角色 {user!r} 不存在。用 superuser 建角色，"
                    "或加 --create-user 让脚本建（需要 CREATEROLE 权限）。",
                    file=sys.stderr,
                )
                return 1
            password = _env("PG_PASSWORD")
            literal = await conn.fetchval("SELECT quote_literal($1)", password)
            await conn.execute(
                f"CREATE ROLE {_ident(user)} LOGIN PASSWORD {literal}"
            )
            print(f"已创建角色 {user}")

        db_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database
        )
        if db_exists:
            print(f"数据库 {database} 已存在，跳过创建")
        else:
            await conn.execute(
                f"CREATE DATABASE {_ident(database)} OWNER {_ident(user)}"
            )
            print(f"已创建数据库 {database}（owner={user}）")
    finally:
        await conn.close()

    print("下一步：./run_bot.sh 启动 bot，启动时会自动建表（schema v11）。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=_env("PG_DB", _DEFAULT_DB),
        help=f"目标库名，默认取 PG_DB（当前 {_env('PG_DB', _DEFAULT_DB)}）",
    )
    parser.add_argument(
        "--create-user",
        action="store_true",
        help="角色不存在时用 PG_PASSWORD 创建（需要 CREATEROLE 权限）",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(init(args.database, create_user=args.create_user))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
