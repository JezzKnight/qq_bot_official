"""PgRepository 行为测试（真实 PG，事务隔离由夹具保证）。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from qq_bot.memory.memory_config import BudgetConfig, MemoryConfig, ShortTermConfig


async def test_video_note_roundtrip(repo) -> None:
    await repo.save_video_note(
        video_key="bilibili:BV1test:p1",
        platform="bilibili",
        video_id="BV1test",
        source_url="https://www.bilibili.com/video/BV1test",
        title="测试视频",
        duration=42.0,
        note="这是视频笔记",
        frame_count=8,
        subtitle_chars=120,
        metadata={"warnings": []},
    )
    row = await repo.get_video_note("bilibili:BV1test:p1")
    assert row is not None
    assert row["title"] == "测试视频"
    assert row["note"] == "这是视频笔记"
    assert row["metadata"] == {"warnings": []}
    assert row["hit_count"] == 1

    second = await repo.get_video_note("bilibili:BV1test:p1")
    assert second is not None
    assert second["hit_count"] == 2


async def test_ensure_thread_is_idempotent_for_private(repo) -> None:
    first = await repo.ensure_thread("private", None, "U42")
    second = await repo.ensure_thread("private", None, "U42")
    assert first == second


async def test_ensure_thread_is_idempotent_for_group(repo) -> None:
    a = await repo.ensure_thread("group_member", "G100", "U7")
    b = await repo.ensure_thread("group_member", "G100", "U7")
    assert a == b
    other = await repo.ensure_thread("group_member", "G100", "U8")
    assert other != a


async def test_append_and_history_ordered(repo) -> None:
    tid = await repo.ensure_thread("group_member", "G100", "U7")
    await repo.append(
        thread_id=tid,
        group_id="G100",
        sender_id="U7",
        role="user",
        content="第一句",
        sender_name="小明",
    )
    await repo.append(
        thread_id=tid,
        group_id="G100",
        sender_id=None,
        role="assistant",
        content="第一句回复",
    )
    history = await repo.history(tid, 10)
    assert [m.content for m in history] == ["第一句", "第一句回复"]
    assert history[0].sender_name == "小明"
    assert history[1].sender_name is None


async def test_group_window_excludes_own_thread(repo) -> None:
    mine = await repo.ensure_thread("group_member", "G100", "U7")
    other = await repo.ensure_thread("group_member", "G100", "U8")
    await repo.append(
        thread_id=mine,
        group_id="G100",
        sender_id="U7",
        role="user",
        content="我说的",
        sender_name="小明",
    )
    await repo.append(
        thread_id=other,
        group_id="G100",
        sender_id="U8",
        role="user",
        content="别人说的",
        sender_name="阿强",
    )
    window = await repo.group_window("G100", mine, 10)
    assert [m.content for m in window] == ["别人说的"]


async def test_messages_by_date_supports_openid_group(repo) -> None:
    tid = await repo.ensure_thread(
        "group_member", "C69128F2D1D26F8285AAF24C0BA3576F", "U7"
    )
    await repo.append(
        thread_id=tid,
        group_id="C69128F2D1D26F8285AAF24C0BA3576F",
        sender_id="U7",
        role="user",
        content="openid 群消息",
        sender_name="小明",
    )
    rows = await repo.messages_by_date(
        date_str=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        limit=10,
        group_id="C69128F2D1D26F8285AAF24C0BA3576F",
    )
    assert any(row["content"] == "openid 群消息" for row in rows)


async def test_clear_thread_isolated(repo) -> None:
    mine = await repo.ensure_thread("group_member", "G100", "U7")
    other = await repo.ensure_thread("group_member", "G100", "U8")
    for tid, uid in ((mine, "U7"), (other, "U8")):
        await repo.append(
            thread_id=tid, group_id="G100", sender_id=uid, role="user", content="hi"
        )
    deleted = await repo.clear_thread(mine)
    assert deleted == 1
    assert await repo.history(mine, 10) == []
    assert len(await repo.history(other, 10)) == 1


async def test_build_context_does_not_overlap(repo) -> None:
    from qq_bot.memory.assembler import build_context

    mine = await repo.ensure_thread("group_member", "G100", "U7")
    other = await repo.ensure_thread("group_member", "G100", "U8")
    await repo.append(
        thread_id=mine,
        group_id="G100",
        sender_id="U7",
        role="user",
        content="我的消息",
        sender_name="小明",
    )
    await repo.append(
        thread_id=other,
        group_id="G100",
        sender_id="U8",
        role="user",
        content="他的消息",
        sender_name="阿强",
    )
    cfg = MemoryConfig(
        short_term=ShortTermConfig(conversation_window=10, thread_window=10),
        budget=BudgetConfig(conversation_window=10_000, thread_window=10_000),
    )
    parts = await build_context(repo, thread_id=mine, group_id="G100", config=cfg)
    texts = [m.content for m in parts.conversation]
    assert texts == ["我的消息", "他的消息"]


async def test_group_context_merges_threads_and_formats_summary(repo) -> None:
    from qq_bot.memory.assembler import build_context

    a = await repo.ensure_thread("group_member", "G200", "U1")
    b = await repo.ensure_thread("group_member", "G200", "U2")
    first = await repo.append(
        thread_id=a, group_id="G200", sender_id="U1", role="user",
        content="我先说", sender_name="小明",
    )
    await repo.append(
        thread_id=b, group_id="G200", sender_id="U2", role="user",
        content="他再说", sender_name="阿强",
    )
    last = await repo.append(
        thread_id=a, group_id="G200", sender_id="U1", role="assistant",
        content="bot 回一句", sender_name=None,
    )
    times = await repo.message_time_range("G200", first, last)
    assert times is not None
    await repo.insert_summary(
        group_id="G200",
        content="他们把钓鱼的事聊完了。",
        covers_from=first,
        covers_to=last,
        covers_from_at=times[0],
        covers_to_at=times[1],
    )
    cfg = MemoryConfig(
        short_term=ShortTermConfig(conversation_window=10, thread_window=10),
        budget=BudgetConfig(conversation_window=10_000, thread_window=10_000),
    )
    parts = await build_context(repo, thread_id=a, group_id="G200", config=cfg)
    assert [m.content for m in parts.conversation] == ["我先说", "他再说", "bot 回一句"]
    assert parts.summary is not None
    assert parts.summary.startswith("这段摘要发生的时间在<")
    assert "仅供参考" in parts.summary
    assert "他们把钓鱼的事聊完了。" in parts.summary


async def test_group_context_moves_note_rows_to_ledger(repo) -> None:
    from qq_bot.memory.assembler import build_context

    tid = await repo.ensure_thread("group_member", "G300", "U1")
    await repo.append(
        thread_id=tid, group_id="G300", sender_id="U1", role="user",
        content="钓了几斤", sender_name="小明",
    )
    await repo.append(
        thread_id=tid, group_id="G300", sender_id="U1", role="assistant",
        content="三斤", sender_name=None,
    )
    await repo.append(
        thread_id=tid, group_id="G300", sender_id="U1", role="assistant",
        content="[检索记录] 这条回复查过网：钓鱼", sender_name=None,
    )
    cfg = MemoryConfig(
        short_term=ShortTermConfig(conversation_window=10, thread_window=10),
        budget=BudgetConfig(conversation_window=10_000, thread_window=10_000),
    )
    parts = await build_context(repo, thread_id=tid, group_id="G300", config=cfg)
    assert [m.content for m in parts.conversation] == ["钓了几斤", "三斤"]
    assert parts.notes == "[检索记录] 这条回复查过网：钓鱼"


async def test_duplicate_external_id_is_ignored(repo) -> None:
    tid = await repo.ensure_thread("group_member", "G100", "U7")
    first = await repo.append(
        thread_id=tid,
        group_id="G100",
        sender_id="U7",
        role="user",
        content="同一句",
        sender_name="小明",
        external_id="M1",
    )
    second = await repo.append(
        thread_id=tid,
        group_id="G100",
        sender_id="U7",
        role="user",
        content="同一句",
        sender_name="小明",
        external_id="M1",
    )
    assert first > 0
    assert second == 0
    history = await repo.history(tid, 10)
    assert [m.content for m in history] == ["同一句"]
    assert history[0].external_id == "M1"


async def test_build_context_excludes_current_message(repo) -> None:
    from qq_bot.memory.assembler import build_context

    tid = await repo.ensure_thread("group_member", "G100", "U7")
    await repo.append(
        thread_id=tid,
        group_id="G100",
        sender_id="U7",
        role="user",
        content="当前这句",
        sender_name="小明",
        external_id="M-now",
    )
    cfg = MemoryConfig(
        short_term=ShortTermConfig(thread_window=10),
        budget=BudgetConfig(thread_window=10_000),
    )
    parts = await build_context(
        repo, thread_id=tid, group_id=None, config=cfg, exclude_external_id="M-now"
    )
    assert parts.conversation == []


async def test_budget_trims_oldest() -> None:
    from qq_bot.ai.types import ChatMessage
    from qq_bot.memory.assembler import trim_by_budget

    msgs = [ChatMessage(role="user", content="字" * 100) for _ in range(10)]
    trimmed = trim_by_budget(msgs, budget=250, keep_tail=2)
    assert len(trimmed) == 2


async def test_clear_group_summaries(repo) -> None:
    await repo.insert_summary(
        group_id="G1", content="summary", covers_from=1, covers_to=2
    )
    assert await repo.clear_group_summaries("G1") == 1
    assert await repo.count_summaries("G1") == 0


async def test_clear_group_threads_and_memories(repo) -> None:
    mine = await repo.ensure_thread("group_member", "G9", "U1")
    other = await repo.ensure_thread("group_member", "G9", "U2")
    keep = await repo.ensure_thread("group_member", "G10", "U1")
    await repo.append(
        thread_id=mine, group_id="G9", sender_id="U1", role="user", content="hi"
    )
    await repo.append(
        thread_id=other, group_id="G9", sender_id="U2", role="user", content="yo"
    )
    await repo.append(
        thread_id=keep, group_id="G10", sender_id="U1", role="user", content="hey"
    )
    await repo.upsert_memory(
        scope_kind="group", scope_id="G9", key="k", content="v", mem_type="fact"
    )
    await repo.upsert_memory(
        scope_kind="group", scope_id="G10", key="k", content="v", mem_type="fact"
    )

    assert await repo.clear_group_threads("G9") == (2, 2)
    assert await repo.clear_group_memories("G9") == 1
    assert await repo.history(mine, 10) == []
    assert await repo.history(keep, 10)
    assert await repo.list_memories(user_id="U1", group_id="G9") == []
    assert await repo.list_memories(user_id="U1", group_id="G10")


async def test_recent_group_messages_across_threads(repo) -> None:
    t1 = await repo.ensure_thread("group_member", "G900", "U1")
    t2 = await repo.ensure_thread("group_member", "G900", "U2")
    await repo.append(
        thread_id=t1,
        group_id="G900",
        sender_id="U1",
        role="user",
        content="三斤",
        sender_name="小明",
    )
    await repo.append(
        thread_id=t2,
        group_id="G900",
        sender_id="U2",
        role="user",
        content="五斤",
        sender_name="阿强",
    )
    await repo.append(
        thread_id=t1,
        group_id="G900",
        sender_id=None,
        role="assistant",
        content="牛啊",
    )
    rows = await repo.recent_group_messages("G900")
    assert [m.content for m in rows] == ["三斤", "五斤", "牛啊"]
    assert rows[0].sender_id == "U1"
    assert rows[1].sender_name == "阿强"
    assert rows[2].role == "assistant"
