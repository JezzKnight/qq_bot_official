"""init_db 脚本的纯函数：标识符校验与 DSN 拼装。"""

from __future__ import annotations

import pytest

from scripts.init_db import _dsn, _ident


def test_ident_quotes_normal_name() -> None:
    assert _ident("qq_bot_official") == '"qq_bot_official"'


def test_ident_rejects_injection() -> None:
    with pytest.raises(ValueError):
        _ident('x"; DROP DATABASE postgres; --')


def test_ident_rejects_leading_digit() -> None:
    with pytest.raises(ValueError):
        _ident("9bot")


def test_dsn_uses_pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_USER", "u")
    monkeypatch.setenv("PG_PASSWORD", "p@ss")
    monkeypatch.setenv("PG_HOST", "db")
    monkeypatch.setenv("PG_PORT", "5433")
    assert _dsn("qq") == "postgresql://u:p%40ss@db:5433/qq"
