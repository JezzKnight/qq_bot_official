"""回复文本的分段策略（官方 QQ 被动回复上限 5 次）。"""

from __future__ import annotations

MAX_REPLY_PARTS = 4


def split_for_reply(text: str, *, max_parts: int = MAX_REPLY_PARTS) -> list[str]:
    """按空行拆分回复；超过上限时把尾部合并，避免撞官方 5 次上限。"""
    chunks = [c.strip() for c in (text or "").split("\n\n") if c.strip()]
    if not chunks:
        return [""]
    if len(chunks) <= max_parts:
        return chunks
    head = chunks[: max_parts - 1]
    head.append("\n\n".join(chunks[max_parts - 1 :]))
    return head
