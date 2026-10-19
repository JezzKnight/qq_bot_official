"""PgRepository 行为测试（真实 PG，事务隔离由夹具保证）。"""

from __future__ import annotations

from qq_bot.memory.memory_config import BudgetConfig, MemoryConfig, ShortTermConfig


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
        thread_id=tid, group_id="G100", sender_id="U7", role="user",
        content="第一句", sender_name="小明",
    )
    await repo.append(
        thread_id=tid, group_id="G100", sender_id=None, role="assistant",
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
        thread_id=mine, group_id="G100", sender_id="U7", role="user",
        content="我说的", sender_name="小明",
    )
    await repo.append(
        thread_id=other, group_id="G100", sender_id="U8", role="user",
        content="别人说的", sender_name="阿强",
    )
    window = await repo.group_window("G100", mine, 10)
    assert [m.content for m in window] == ["别人说的"]


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
        thread_id=mine, group_id="G100", sender_id="U7", role="user",
        content="我的消息", sender_name="小明",
    )
    await repo.append(
        thread_id=other, group_id="G100", sender_id="U8", role="user",
        content="他的消息", sender_name="阿强",
    )
    cfg = MemoryConfig(
        short_term=ShortTermConfig(group_window=10, thread_window=10),
        budget=BudgetConfig(group_window=10_000, thread_window=10_000),
    )
    parts = await build_context(repo, thread_id=mine, group_id="G100", config=cfg)
    mine_text = [m.content for m in parts.thread_window]
    other_text = [m.content for m in parts.group_window]
    assert mine_text == ["我的消息"]
    assert other_text == ["他的消息"]
    assert not set(mine_text) & set(other_text)


async def test_budget_trims_oldest() -> None:
    from qq_bot.ai.types import ChatMessage
    from qq_bot.memory.assembler import trim_by_budget

    msgs = [ChatMessage(role="user", content="字" * 100) for _ in range(10)]
    trimmed = trim_by_budget(msgs, budget=250, keep_tail=2)
    assert len(trimmed) == 2
