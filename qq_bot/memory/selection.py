"""长期记忆的注入选取与格式化（纯函数，便于单测）。

动机：只按 confidence 排序取前 N，会让高置信旧记忆长期占满名额、新记忆永远进不来
（自审发现的问题）。这里改成「近期 N 条 + 高置信 M 条」去重合并。
"""

from __future__ import annotations

RECENT = 4
TOP = 8


def pick_memories(
    rows: list[dict], *, recent: int = RECENT, top: int = TOP
) -> list[dict]:
    """近期 N 条与高置信 M 条的并集；按 key 去重，高置信优先在前。"""
    if not rows:
        return []
    by_recency = sorted(
        rows, key=lambda r: r["last_confirmed_at"], reverse=True
    )[:recent]
    by_score = sorted(
        rows,
        key=lambda r: (r["confidence"], r["last_confirmed_at"]),
        reverse=True,
    )[:top]
    seen = {r["key"] for r in by_score}
    return by_score + [r for r in by_recency if r["key"] not in seen]


def format_memory_block(header: str, rows: list[dict]) -> str:
    """header + 每条 [key] content。"""
    lines = [header]
    lines.extend(f"[{r['key']}] {r['content']}".rstrip() for r in rows)
    return "\n".join(lines)
