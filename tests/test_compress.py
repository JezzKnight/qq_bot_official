"""群摘要压缩：轮次触发、区间连续、幂等、全量重建、并发去重。"""

from __future__ import annotations

import asyncio

from qq_bot.memory.compress import compress_if_needed
from qq_bot.memory.memory_config import (
    CompressionConfig,
    MemoryConfig,
    ShortTermConfig,
)


def _config(
    *, trigger: int = 3, window: int = 2, rebuild_every: int = 10
) -> MemoryConfig:
    return MemoryConfig(
        short_term=ShortTermConfig(group_window=window, thread_window=10),
        compression=CompressionConfig(
            trigger_turns=trigger,
            summary_max_tokens=100,
            full_rebuild_every=rebuild_every,
        ),
    )


async def _seed(
    repo, gid: str, uid: str, count: int, *, name: str = "小明"
) -> list[int]:
    tid = await repo.ensure_thread("group_member", gid, uid)
    ids = []
    for i in range(count):
        ids.append(  # noqa: PERF401 —— 每次 append 都是 await，无法写成推导式
            await repo.append(
                thread_id=tid,
                group_id=gid,
                sender_id=uid,
                role="user",
                content=f"第{i}条",
                sender_name=name,
            )
        )
    return ids


async def fake_summary(previous, messages, _max_tokens) -> str:
    head = previous or ""
    tail = ";".join(f"{m['sender_name']}:{m['content']}" for m in messages)
    return f"{head}[{tail}]"


async def test_below_threshold_does_nothing(repo) -> None:
    await _seed(repo, "G100", "U7", 2)
    assert (
        await compress_if_needed(
            repo, group_id="G100", config=_config(trigger=3), summarize=fake_summary
        )
        is None
    )
    assert await repo.count_summaries("G100") == 0


async def test_compress_covers_window_outside(repo) -> None:
    ids = await _seed(repo, "G100", "U7", 5)
    outcome = await compress_if_needed(
        repo,
        group_id="G100",
        config=_config(trigger=3, window=2),
        summarize=fake_summary,
    )
    assert outcome is not None
    # 窗口（最后 2 条）保留原文，压缩区间止于第 3 条
    assert outcome.covers_from == ids[0]
    assert outcome.covers_to == ids[2]
    assert outcome.messages == 3
    assert await repo.count_summaries("G100") == 1


async def test_second_batch_is_contiguous(repo) -> None:
    await _seed(repo, "G100", "U7", 5)
    first = await compress_if_needed(
        repo,
        group_id="G100",
        config=_config(trigger=3, window=2),
        summarize=fake_summary,
    )
    assert first is not None
    ids2 = await _seed(repo, "G100", "U7", 3)
    outcome = await compress_if_needed(
        repo,
        group_id="G100",
        config=_config(trigger=3, window=2),
        summarize=fake_summary,
    )
    assert outcome is not None
    # 关键性质：第二批从第一批结束的下一条开始，不重叠；且窗口内最后 2 条不被覆盖
    assert outcome.covers_from == first.covers_to + 1
    assert outcome.covers_to < ids2[-1]


async def test_idempotent_when_no_new_turns(repo) -> None:
    await _seed(repo, "G100", "U7", 5)
    first = await compress_if_needed(
        repo,
        group_id="G100",
        config=_config(trigger=3, window=2),
        summarize=fake_summary,
    )
    assert first is not None
    again = await compress_if_needed(
        repo,
        group_id="G100",
        config=_config(trigger=3, window=2),
        summarize=fake_summary,
    )
    assert again is None
    assert await repo.count_summaries("G100") == 1


async def test_full_rebuild_after_threshold(repo) -> None:
    await _seed(repo, "G100", "U7", 5)
    await compress_if_needed(
        repo,
        group_id="G100",
        config=_config(trigger=3, window=2, rebuild_every=1),
        summarize=fake_summary,
    )
    await _seed(repo, "G100", "U7", 3)
    outcome = await compress_if_needed(
        repo,
        group_id="G100",
        config=_config(trigger=3, window=2, rebuild_every=1),
        summarize=fake_summary,
    )
    assert outcome is not None
    assert outcome.rebuilt is True
    assert outcome.covers_from == 1


async def test_concurrent_calls_produce_single_summary(repo) -> None:
    await _seed(repo, "G100", "U7", 6)
    cfg = _config(trigger=3, window=2)
    results = await asyncio.gather(
        compress_if_needed(repo, group_id="G100", config=cfg, summarize=fake_summary),
        compress_if_needed(repo, group_id="G100", config=cfg, summarize=fake_summary),
    )
    assert sum(1 for r in results if r is not None) == 1
    assert await repo.count_summaries("G100") == 1


def test_format_transcript_strips_identity_and_labels_bot() -> None:
    from qq_bot.memory.compress import format_transcript

    messages = [
        {
            "role": "user",
            "sender_name": "小明",
            "content": '<user identity id="7" name="小明"/>\n我想吃火锅',
        },
        {"role": "assistant", "sender_name": None, "content": "好啊"},
        {"role": "tool", "sender_name": None, "content": "忽略我"},
    ]
    text = format_transcript(messages)
    assert "小明: 我想吃火锅" in text
    assert "bot: 好啊" in text
    assert "user identity" not in text
    assert "忽略我" not in text
