"""一轮对话落库：两条消息 + 用量字段，失败不抛出。"""

from __future__ import annotations

from types import SimpleNamespace

from qq_bot.chat.persist import persist_turn


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=100, cached_tokens=40, completion_tokens=20, calls=2
    )


async def test_persist_writes_user_and_assistant(repo) -> None:
    thread_id = await repo.ensure_thread("private", None, 3215481708)
    await persist_turn(
        repo,
        thread_id=thread_id,
        group_id=None,
        sender_id=3215481708,
        sender_name="杨神",
        user_text="你好",
        assistant_text="早上好",
        usage=_usage(),
        model="deepseek-v4-flash",
    )
    rows = await repo.pool.fetch(
        "SELECT role, sender_name, content, prompt_tokens, cached_tokens, "
        "completion_tokens, llm_calls, model FROM messages "
        " WHERE thread_id=$1 ORDER BY id",
        thread_id,
    )
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["sender_name"] == "杨神"
    assert rows[1]["content"] == "早上好"
    assert rows[1]["prompt_tokens"] == 100
    assert rows[1]["cached_tokens"] == 40
    assert rows[1]["llm_calls"] == 2
    assert rows[1]["model"] == "deepseek-v4-flash"


async def test_persist_skips_when_memory_missing() -> None:
    # memory=None 时直接返回，不应抛出
    await persist_turn(
        None,
        thread_id=None,
        group_id=None,
        sender_id=1,
        sender_name="x",
        user_text="a",
        assistant_text="b",
    )
