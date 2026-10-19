"""分层 TTL、记忆统计与陈旧过滤。"""

from __future__ import annotations


async def _seed_message(repo, gid: int, uid: int, text: str = "hi") -> int:
    tid = await repo.ensure_thread("group_member", gid, uid)
    return await repo.append(
        thread_id=tid, group_id=gid, sender_id=uid, role="user", content=text
    )


async def test_purge_messages_only_old(repo) -> None:
    old = await _seed_message(repo, 100, 7, "旧消息")
    fresh = await _seed_message(repo, 100, 7, "新消息")
    await repo.pool.execute(
        "UPDATE messages SET created_at = now() - interval '40 days' WHERE id = $1",
        old,
    )
    assert await repo.purge_messages(30) == 1
    remaining = await repo.pool.fetchval("SELECT array_agg(id) FROM messages")
    assert remaining == [fresh]


async def test_purge_summaries_keeps_latest_per_group(repo) -> None:
    for covers_to in (10, 20, 30):
        await repo.insert_summary(
            group_id=100,
            content=f"摘要{covers_to}",
            covers_from=1,
            covers_to=covers_to,
        )
    await repo.pool.execute(
        "UPDATE group_summaries SET created_at = now() - interval '400 days'"
    )
    deleted = await repo.purge_summaries(365)
    assert deleted == 2
    assert await repo.count_summaries(100) == 1
    assert await repo.latest_summary(100) == "摘要30"


async def test_memory_stats(repo) -> None:
    await _seed_message(repo, 100, 7)
    await repo.upsert_memory(
        scope_kind="user", scope_id=7, key="k", content="v", mem_type="fact"
    )
    stats = await repo.memory_stats()
    assert stats["messages"] == 1
    assert stats["memories"] == 1


async def test_stale_memory_filtered_from_injection_query(repo) -> None:
    await repo.upsert_memory(
        scope_kind="user", scope_id=7, key="old", content="很久没确认", mem_type="fact"
    )
    await repo.upsert_memory(
        scope_kind="user", scope_id=7, key="new", content="刚确认", mem_type="fact"
    )
    await repo.pool.execute(
        "UPDATE memories SET last_confirmed_at = now() - interval '200 days' "
        " WHERE key = 'old'"
    )
    fresh = await repo.list_memories(user_id=7, group_id=100, fresh_within_days=180)
    assert [r["key"] for r in fresh] == ["new"]
    everything = await repo.list_memories(user_id=7, group_id=100)
    assert {r["key"] for r in everything} == {"old", "new"}
