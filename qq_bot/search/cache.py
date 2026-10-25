"""进程内搜索结果缓存：固定 TTL + LRU，避免同一问题在群里反复调用搜索 API。"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_bot.search.types import SearchItem

DEFAULT_CACHE_SIZE = 128
DEFAULT_CACHE_TTL_SECONDS = 600


@dataclass(frozen=True, slots=True)
class CacheEntry:
    items: tuple[SearchItem, ...]
    created_at: float


def make_cache_key(
    query: str,
    *,
    depth: str,
    max_results: int,
    time_range: str | None,
) -> str:
    """缓存键包含所有会影响结果的参数，避免不同模式的缓存互相污染。"""
    raw = f"{query.strip().lower()}|{depth}|{max_results}|{time_range or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SearchCache:
    """只缓存搜索 API 的原始结果；正文不进缓存，避免过期内容长期占用内存。"""

    def __init__(
        self,
        *,
        max_size: int = DEFAULT_CACHE_SIZE,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, key: str) -> list[SearchItem] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self._ttl_seconds:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return list(entry.items)

    def put(self, key: str, items: list[SearchItem]) -> None:
        if not items:
            return
        self._entries[key] = CacheEntry(items=tuple(items), created_at=time.monotonic())
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
