"""用量：按轮累加、落库到 assistant 消息、按日聚合。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from qq_bot.ai import usage_context


def test_turn_usage_accumulates() -> None:
    usage_context.reset()
    usage_context.add(100, 40, 20)
    usage_context.add(50, 0, 10)
    snap = usage_context.snapshot()
    assert snap.prompt_tokens == 150
    assert snap.cached_tokens == 40
    assert snap.completion_tokens == 30
    assert snap.calls == 2


def test_turn_usage_reset_clears() -> None:
    usage_context.add(1, 1, 1)
    usage_context.reset()
    snap = usage_context.snapshot()
    assert snap.calls == 0 and snap.prompt_tokens == 0


async def test_usage_persisted_and_aggregated(repo) -> None:
    tid = await repo.ensure_thread("group_member", "G100", "U7")
    await repo.append(
        thread_id=tid, group_id="G100", sender_id=None, role="assistant",
        content="回答", prompt_tokens=120, completion_tokens=30,
        cached_tokens=64, llm_calls=2, model="glm-test",
    )
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    summary = await repo.usage_summary(today)
    assert summary["prompt_tokens"] == 120
    assert summary["completion_tokens"] == 30
    assert summary["cached_tokens"] == 64
    assert summary["calls"] == 2
    assert summary["turns"] == 1


async def test_refresh_member_name_updates_cards(repo) -> None:
    await repo.upsert_members(
        "G100", [{"user_id": "U7", "card": "旧名", "nickname": "nick"}]
    )
    changed = await repo.refresh_member_name(
        group_id="G100", user_id="U7", card="新名", nickname="nick"
    )
    assert changed is True
    members = await repo.list_members("G100")
    assert members[0]["card"] == "新名"
    # user_names 里旧名片应被替换
    names = {r["name"] for r in await repo.list_names("G100") if r["user_id"] == "U7"}
    assert "新名" in names and "旧名" not in names

    again = await repo.refresh_member_name(
        group_id="G100", user_id="U7", card="新名", nickname="nick"
    )
    assert again is False
