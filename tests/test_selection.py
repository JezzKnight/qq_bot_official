"""长期记忆注入选取：新记忆不会被高置信旧记忆饿死。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qq_bot.memory.selection import pick_memories

NOW = datetime.now(timezone.utc)


def _row(key: str, *, confidence: float, age_days: int) -> dict:
    return {
        "key": key,
        "content": key,
        "confidence": confidence,
        "last_confirmed_at": NOW - timedelta(days=age_days),
    }


def test_recent_low_confidence_is_included() -> None:
    rows = [
        _row(f"old-{i}", confidence=0.95, age_days=200) for i in range(10)
    ] + [_row("new", confidence=0.5, age_days=0)]
    picked = pick_memories(rows)
    keys = {r["key"] for r in picked}
    assert "new" in keys
    assert sum(1 for k in keys if k.startswith("old-")) <= 8


def test_dedup_between_recent_and_top() -> None:
    rows = [
        _row("a", confidence=0.9, age_days=0),
        _row("b", confidence=0.8, age_days=1),
    ]
    picked = pick_memories(rows)
    assert [r["key"] for r in picked] == ["a", "b"]


def test_empty() -> None:
    assert pick_memories([]) == []
