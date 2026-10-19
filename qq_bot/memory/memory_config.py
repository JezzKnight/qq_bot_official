"""记忆子系统配置：读 ``memory.yaml``（与 model.yaml 同款模式）。

数据库连接参数来自环境变量 PG_*（.env 由插件入口 load_dotenv 导出），
也可在 yaml 里用 ``dsn`` 直接覆盖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = "memory.yaml"
ENV_CONFIG_PATH = "MEMORY_CONFIG"

_BACKENDS = frozenset({"postgres", "sqlite"})


class MemoryConfigError(Exception):
    """memory.yaml 缺失或格式非法。"""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class ShortTermConfig:
    group_window: int = 20
    thread_window: int = 12


@dataclass(frozen=True)
class CompressionConfig:
    trigger_turns: int = 30
    summary_max_tokens: int = 500
    model: str = "deepseek-v4-flash"
    full_rebuild_every: int = 10


@dataclass(frozen=True)
class BudgetConfig:
    long_term: int = 800
    group_summary: int = 600
    group_window: int = 1500
    thread_window: int = 2500


@dataclass(frozen=True)
class RetentionConfig:
    raw_days: int = 30
    summary_days: int = 365
    memory_stale_days: int = 180


@dataclass(frozen=True)
class MemoryConfig:
    backend: str = "postgres"
    dsn: str = ""
    short_term: ShortTermConfig = field(default_factory=ShortTermConfig)
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)

    def connection_dsn(self) -> str:
        """返回 asyncpg 可用的 DSN；dsn 为空时用 PG_* 环境变量拼装。"""
        if self.dsn:
            return self.dsn
        user = _env("PG_USER", "postgres")
        password = quote(_env("PG_PASSWORD"), safe="")
        host = _env("PG_HOST", "127.0.0.1")
        port = _env("PG_PORT", "5432")
        database = _env("PG_DB", "qq_bot")
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _as_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        msg = f"memory.yaml 的 {key} 必须是整数: {value!r}"
        raise MemoryConfigError(msg) from exc


def _resolve_path(path: str | Path | None) -> Path:
    raw = str(path or os.getenv(ENV_CONFIG_PATH, DEFAULT_CONFIG_FILE)).strip()
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else _REPO_ROOT / candidate


def load_memory_config(path: str | Path | None = None) -> MemoryConfig:
    """读取 memory.yaml；文件缺失时返回默认值（backend=postgres）。"""
    cfg_path = _resolve_path(path)
    if not cfg_path.is_file():
        return MemoryConfig()
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"memory.yaml 解析失败: {exc}"
        raise MemoryConfigError(msg) from exc
    if not isinstance(data, dict):
        msg = "memory.yaml 顶层必须是映射（mapping）"
        raise MemoryConfigError(msg)

    backend = str(data.get("backend") or "postgres").strip()
    if backend not in _BACKENDS:
        msg = f"memory.yaml 的 backend 只能是 {sorted(_BACKENDS)}: {backend!r}"
        raise MemoryConfigError(msg)

    short = data.get("short_term") or {}
    comp = data.get("compression") or {}
    budget = data.get("budget") or {}
    retention = data.get("retention") or {}
    base = ShortTermConfig()
    comp_base = CompressionConfig()
    budget_base = BudgetConfig()
    ret_base = RetentionConfig()

    return MemoryConfig(
        backend=backend,
        dsn=str(data.get("dsn") or "").strip(),
        short_term=ShortTermConfig(
            group_window=_as_int(short, "group_window", base.group_window),
            thread_window=_as_int(short, "thread_window", base.thread_window),
        ),
        compression=CompressionConfig(
            trigger_turns=_as_int(comp, "trigger_turns", comp_base.trigger_turns),
            summary_max_tokens=_as_int(
                comp, "summary_max_tokens", comp_base.summary_max_tokens
            ),
            model=str(comp.get("model") or comp_base.model),
            full_rebuild_every=_as_int(
                comp, "full_rebuild_every", comp_base.full_rebuild_every
            ),
        ),
        budget=BudgetConfig(
            long_term=_as_int(budget, "long_term", budget_base.long_term),
            group_summary=_as_int(
                budget, "group_summary", budget_base.group_summary
            ),
            group_window=_as_int(budget, "group_window", budget_base.group_window),
            thread_window=_as_int(budget, "thread_window", budget_base.thread_window),
        ),
        retention=RetentionConfig(
            raw_days=_as_int(retention, "raw_days", ret_base.raw_days),
            summary_days=_as_int(retention, "summary_days", ret_base.summary_days),
            memory_stale_days=_as_int(
                retention, "memory_stale_days", ret_base.memory_stale_days
            ),
        ),
    )


class _State:
    config: MemoryConfig | None = None


_state = _State()


def get_memory_config() -> MemoryConfig:
    if _state.config is None:
        _state.config = load_memory_config()
    return _state.config


def reload_memory_config(path: str | Path | None = None) -> MemoryConfig:
    _state.config = load_memory_config(path)
    return _state.config
