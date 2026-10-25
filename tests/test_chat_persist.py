"""一轮对话落库：两条消息 + 用量字段，失败不抛出。"""

from __future__ import annotations

from types import SimpleNamespace

from qq_bot.chat.persist import persist_turn


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=100, cached_tokens=40, completion_tokens=20, calls=2
    )


async def test_persist_writes_user_and_assistant(repo) -> None:
    thread_id = await repo.ensure_thread("private", None, "U3215481708")
    await persist_turn(
        repo,
        thread_id=thread_id,
        group_id=None,
        sender_id="U3215481708",
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
        sender_id="U1",
        sender_name="x",
        user_text="a",
        assistant_text="b",
    )


async def test_persist_writes_extra_assistant_note(repo) -> None:
    thread_id = await repo.ensure_thread("private", None, "U-extra")
    await persist_turn(
        repo,
        thread_id=thread_id,
        group_id=None,
        sender_id="U-extra",
        sender_name="测试",
        user_text="看看这个视频",
        assistant_text="看完了",
        extra_assistant_text="[视频分析记录]\n标题：测试视频\n暗猫是碧蓝幻想角色",
    )
    rows = await repo.pool.fetch(
        "SELECT role, content FROM messages WHERE thread_id=$1 ORDER BY id", thread_id
    )
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
    assert "暗猫是碧蓝幻想角色" in rows[2]["content"]


async def test_persist_writes_tool_trace_in_order(repo) -> None:
    from qq_bot.ai.types import ChatMessage

    thread_id = await repo.ensure_thread("private", None, "U3215481708")
    trace = [
        ChatMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"query":"CS2"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="[S1] Vitality 2-1", tool_call_id="call_1"),
    ]
    await persist_turn(
        repo,
        thread_id=thread_id,
        group_id=None,
        sender_id="U3215481708",
        sender_name="杨神",
        user_text="查一下",
        assistant_text="赢了",
        usage=_usage(),
        model="deepseek-v4-flash",
        tool_trace=trace,
    )
    rows = await repo.pool.fetch(
        "SELECT role, content, tool_calls, tool_call_id FROM messages "
        " WHERE thread_id=$1 ORDER BY id",
        thread_id,
    )
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[1]["tool_calls"][0]["id"] == "call_1"
    assert rows[1]["content"] == ""
    assert rows[2]["tool_call_id"] == "call_1"
    assert rows[2]["content"] == "[S1] Vitality 2-1"


def test_repair_tool_pairs_keeps_pair_when_window_starts_mid_turn() -> None:
    from qq_bot.ai.types import ChatMessage
    from qq_bot.memory.assembler import repair_tool_pairs

    pair = [
        ChatMessage(
            role="assistant",
            tool_calls=[{"id": "c1", "type": "function", "function": {}}],
            content="",
        ),
        ChatMessage(role="tool", content="结果" * 100, tool_call_id="c1"),
    ]
    messages = [ChatMessage(role="user", content="问"), *pair]
    trimmed = repair_tool_pairs(messages)
    assert [m.role for m in trimmed] == ["user", "assistant", "tool"]


def test_repair_tool_pairs_drops_orphan_tool() -> None:
    from qq_bot.ai.types import ChatMessage
    from qq_bot.memory.assembler import repair_tool_pairs

    messages = [ChatMessage(role="tool", content="孤儿", tool_call_id="c1")]
    assert repair_tool_pairs(messages) == []


def test_repair_tool_pairs_drops_trailing_call_without_result() -> None:
    from qq_bot.ai.types import ChatMessage
    from qq_bot.memory.assembler import repair_tool_pairs

    messages = [
        ChatMessage(role="user", content="问"),
        ChatMessage(
            role="assistant",
            tool_calls=[{"id": "c1", "type": "function", "function": {}}],
            content="",
        ),
    ]
    trimmed = repair_tool_pairs(messages)
    assert [m.role for m in trimmed] == ["user"]
