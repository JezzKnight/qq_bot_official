"""QQ 被动回复窗口登记表：自动接话只能「蹭」已经打开的被动窗口。

平台事实：同一条 @ 消息打开的被动回复窗口是 5 分钟、最多 5 次回复。一条 @
触发的回复最多用掉 4 段（``QQReply.max_parts``），所以窗口里通常只剩 1 次。

这里登记的是「这条 msg_id 已经发过几条」，不是「还能发几条」：
``register`` 默认按「@ 那一轮用满 4 段」记账，``acquire`` 只在
``used < MAX_PASSIVE_REPLIES`` 时放行 —— 一个窗口最多接一句。宁可少说，也
不能超发：平台超额会直接拒发，用户看到的是机器人突然哑掉。

纯内存 + 可注入单调时钟，单测不需要 sleep。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

MAX_PASSIVE_REPLIES = 5  # 平台硬上限：同一条 msg_id 5 分钟内最多 5 次被动回复
WINDOW_SECONDS = 300.0  # 平台窗口长度：5 分钟
DEFAULT_USED = MAX_PASSIVE_REPLIES - 1  # 假设 @ 那一轮已用满 4 段，只留 1 次
MAX_WINDOWS = 512  # 群数上限；超过时淘汰最早登记的窗口，避免字典无限增长


@dataclass
class _Window:
    """一条 @ 消息打开的被动回复窗口。"""

    reply: Any
    expire_at: float
    used: int
    bot_last_at: float


_windows: dict[str, _Window] = {}


def _clock(now: float | None) -> float:
    return time.monotonic() if now is None else now


def register(
    group_key: str,
    reply: Any,
    *,
    used: int = DEFAULT_USED,
    window_seconds: float = WINDOW_SECONDS,
    now: float | None = None,
) -> None:
    """登记（或刷新）一个群的被动窗口。

    ``used`` 是「已经发出去几条」的保守估计：默认 4 表示 @ 那一轮按上限记账，
    自动接话只剩 1 次；传 0 可以放开到 5 次（仅调试用，线上会超额）。
    """
    if not group_key:
        return
    clock = _clock(now)
    clear_expired(clock)
    if len(_windows) >= MAX_WINDOWS and group_key not in _windows:
        _evict_oldest()
    _windows[group_key] = _Window(
        reply=reply,
        expire_at=clock + window_seconds,
        used=max(0, used),
        bot_last_at=clock,
    )


def acquire(group_key: str, *, now: float | None = None) -> Any | None:
    """取一个还能用的被动回复通道；没窗口、过期、额度用尽都返回 None。"""
    clock = _clock(now)
    window = _windows.get(group_key)
    if window is None:
        return None
    if window.expire_at <= clock:
        _windows.pop(group_key, None)
        return None
    if window.used >= MAX_PASSIVE_REPLIES:
        return None
    return window.reply


def mark_used(group_key: str, parts: int = 1, *, now: float | None = None) -> None:
    """记一条已发出的被动回复；窗口已过期则顺手丢掉，不再记账。"""
    window = _windows.get(group_key)
    if window is None:
        return
    if window.expire_at <= _clock(now):
        _windows.pop(group_key, None)
        return
    window.used += max(0, parts)


def minutes_since_bot_spoke(
    group_key: str, *, now: float | None = None
) -> float | None:
    """窗口登记至今多少分钟（约等于 bot 上次在这个群说话的时间）。"""
    window = _windows.get(group_key)
    if window is None:
        return None
    return max(0.0, (_clock(now) - window.bot_last_at) / 60.0)


def clear_expired(now: float | None = None) -> None:
    """惰性清理：读/写时顺手扫一遍，不另起后台任务。"""
    clock = _clock(now)
    for key in [key for key, window in _windows.items() if window.expire_at <= clock]:
        del _windows[key]


def reset() -> None:
    """清空登记表（仅测试用；线上靠 TTL 自然过期）。"""
    _windows.clear()


def _evict_oldest() -> None:
    if not _windows:
        return
    oldest = min(_windows, key=lambda key: _windows[key].bot_last_at)
    del _windows[oldest]
