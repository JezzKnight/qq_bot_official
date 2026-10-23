"""会话级串行：同一会话的两轮回复不能交叉。

分段发送之后，一屏里会连着出现多条消息。如果两次用户消息分别开了两个 turn
且各自在发，消息就会串台（"对甲的回答"和"对乙的回答"交替出现）。这里按会话
给一把锁，后来的那一轮排队等前一轮发送完。
"""

from __future__ import annotations

import asyncio

_MAX_LOCKS = 512  # 会话数上限：超过时清理空闲锁，避免字典无限增长
_locks: dict[str, asyncio.Lock] = {}


def group_session_key(group_key: str | None, user_key: str) -> str:
    """会话标识：群聊按群串行，私聊按用户串行。"""
    return f"group:{group_key}" if group_key else f"user:{user_key}"


def session_lock(session_key: str) -> asyncio.Lock:
    """取该会话的锁；锁对象复用，空闲过多时才回收。"""
    lock = _locks.get(session_key)
    if lock is not None:
        return lock
    if len(_locks) >= _MAX_LOCKS:
        for key, idle in list(_locks.items()):
            if not idle.locked():
                del _locks[key]
    lock = asyncio.Lock()
    _locks[session_key] = lock
    return lock
