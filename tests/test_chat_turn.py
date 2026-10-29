"""run_turn：私聊/群聊主链路（假客户端 + 真实测试库）。"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from qq_bot.chat import turn as turn_mod
from qq_bot.platform.ports import Incoming, PlatformCaps
from qq_bot.platform.qqbot.incoming import QQ_CAPS


class FakeReply:
    max_parts = 1000

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.finished = False

    async def text(self, content: str) -> None:
        self.sent.append(("text", content))

    async def passive(self, content: str) -> None:
        self.sent.append(("passive", content))

    async def mention(self, user_key: str, content: str) -> None:
        self.sent.append(("mention", f"{user_key}|{content}"))

    async def image(self, _data: bytes, name: str = "") -> None:
        self.sent.append(("image", name))

    async def finish(self) -> None:
        self.finished = True


class FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def chat(self, **kwargs):  # noqa: ARG002
        self.calls += 1
        return SimpleNamespace(
            content=self.content,
            tool_calls=None,
            reasoning_content=None,
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=3,
        )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        memory_injection_enabled=False,
        glossary_enabled=False,
        max_context_tokens=4000,
        reply_typing_delay=False,
        bot_self_id="999",
    )


@pytest.fixture
def patched(monkeypatch, repo):
    client = FakeClient("你好呀")
    monkeypatch.setattr(turn_mod, "get_memory", lambda: _async_value(repo))
    monkeypatch.setattr(
        turn_mod, "get_client_for_model", lambda _m: _async_value(client)
    )
    monkeypatch.setattr(turn_mod, "default_model_name", lambda: "test-model")
    monkeypatch.setattr(
        turn_mod,
        "get_model_params",
        lambda _m: SimpleNamespace(temperature=0.7, max_tokens=100),
    )
    monkeypatch.setattr(turn_mod, "get_session_model", lambda _s, _d: "test-model")
    monkeypatch.setattr(turn_mod, "catalog_text", lambda: "")
    monkeypatch.setattr(
        turn_mod, "build_private_injection", lambda _uid: _async_value("")
    )
    monkeypatch.setattr(
        turn_mod, "build_memory_injection", lambda *_a, **_k: _async_value(("", []))
    )
    monkeypatch.setattr(turn_mod, "schedule_compress", lambda _gid: None)
    return client


def _async_value(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner()


def _incoming(reply: FakeReply, *, group: str | None = None) -> Incoming:
    return Incoming(
        user_key="openid-12345",
        group_key=group,
        display_name="测试用户",
        text="你好",
        images=[],
        message_id="1",
        reply=reply,
        caps=QQ_CAPS,
    )


async def test_private_turn_persists_and_replies(patched) -> None:  # noqa: ARG001
    reply = FakeReply()
    await turn_mod.run_turn(_incoming(reply), config=_config())
    assert reply.finished is True
    assert ("text", "你好呀") in reply.sent
    rows = await patched_repo_rows()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "你好呀"


async def test_group_turn_mentions_sender(patched) -> None:  # noqa: ARG001
    reply = FakeReply()
    await turn_mod.run_turn(_incoming(reply, group="795245301"), config=_config())
    assert reply.finished is True
    assert reply.sent[0][0] == "mention"
    assert reply.sent[0][1].startswith("openid-12345|")


class RetryClient:
    def __init__(self) -> None:
        self.responses = [
            SimpleNamespace(
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"query": "赛马娘 2026年11月 大赛"}',
                        },
                    }
                ],
                reasoning_content=None,
                prompt_tokens=10,
                cached_tokens=0,
                completion_tokens=3,
            ),
            SimpleNamespace(
                content="没查到 2026 年 11 月的可靠资料",
                tool_calls=None,
                reasoning_content=None,
                prompt_tokens=10,
                cached_tokens=0,
                completion_tokens=3,
            ),
            SimpleNamespace(
                content="补查后的答案",
                tool_calls=None,
                reasoning_content=None,
                prompt_tokens=10,
                cached_tokens=0,
                completion_tokens=3,
            ),
        ]

    async def chat(self, **_kwargs):
        return self.responses.pop(0)


async def test_turn_retries_once_after_insufficient_search(
    patched,  # noqa: ARG001
    monkeypatch,
) -> None:
    client = RetryClient()
    retry_calls: list[str] = []
    retry_snapshots: list[tuple[tuple[str, str], ...]] = []

    async def fake_search(**_kwargs):
        return "[S1] 旧证据"

    async def fake_retry(query: str) -> str:
        retry_calls.append(query)
        retry_snapshots.append(tuple(reply.sent))
        return "[S1] 新证据：2026年11月是 LoH"

    monkeypatch.setattr(
        turn_mod, "get_client_for_model", lambda _m: _async_value(client)
    )
    monkeypatch.setattr(turn_mod, "get_tools_schema", lambda *_names: [])
    monkeypatch.setattr(turn_mod, "TOOLS", {"search": {"func": fake_search}})
    monkeypatch.setattr(turn_mod, "retry_search", fake_retry)

    reply = FakeReply()
    result = await turn_mod.run_turn(_incoming(reply), config=_config())

    assert result == "补查后的答案"
    assert retry_calls == ["赛马娘 2026年11月 大赛"]
    assert retry_snapshots == [(("passive", "这个我得好好查查"),)]
    assert reply.sent[0] == ("passive", "这个我得好好查查")
    assert ("text", "补查后的答案") in reply.sent
    assert reply.sent.index(("passive", "这个我得好好查查")) < reply.sent.index(
        ("text", "补查后的答案")
    )


async def patched_repo_rows():
    # 借助 turn_mod.get_memory 的返回值拿仓储（fixture 已把它指向 repo）
    repo = await turn_mod.get_memory()
    return await repo.pool.fetch("SELECT role, content FROM messages ORDER BY id")


def test_compression_skipped_without_full_group_access() -> None:
    incoming = dataclasses.replace(
        _incoming(FakeReply()),
        caps=PlatformCaps(
            can_see_all_group_messages=False,
            can_send_image=True,
            max_reply_parts=4,
        ),
    )
    assert turn_mod._compression_scheduler(incoming) is None


def test_compression_enabled_with_full_group_access() -> None:
    incoming = dataclasses.replace(
        _incoming(FakeReply()),
        caps=PlatformCaps(
            can_see_all_group_messages=True,
            can_send_image=True,
            max_reply_parts=4,
        ),
    )
    assert turn_mod._compression_scheduler(incoming) is not None


class QuoteReply(FakeReply):
    def __init__(self) -> None:
        super().__init__()
        self.quoted: list[tuple[str, str]] = []

    async def quote(self, message_id: str, content: str) -> None:
        self.quoted.append((message_id, content))


def _observe_pair(group: str) -> None:
    from qq_bot.chat import conversation

    conversation.manager.reset()
    conversation.manager.observe(
        group_key=group,
        user_key="openid-a",
        name="小明",
        text="今晚吃什么",
        message_id="m1",
        at=100.0,
    )
    conversation.manager.observe(
        group_key=group,
        user_key="openid-b",
        name="AMelax",
        text="随便",
        message_id="m2",
        at=101.0,
    )


async def test_auto_quote_when_newer_message_arrived(patched) -> None:  # noqa: ARG001
    group = "795245302"
    _observe_pair(group)
    reply = QuoteReply()
    incoming = dataclasses.replace(
        _incoming(reply, group=group), message_id="m1", auto_reply=True
    )
    config = _config()
    config.quote_reply_enabled = True
    config.quote_race_enabled = True
    config.query_markdown_enabled = False
    try:
        await turn_mod.run_turn(incoming, config=config)
    finally:
        from qq_bot.chat import conversation

        conversation.manager.reset()
    assert reply.quoted == [("m1", "你好呀")]
    assert ("text", "你好呀") not in reply.sent


async def test_no_auto_quote_when_trigger_is_latest(patched) -> None:  # noqa: ARG001
    group = "795245303"
    _observe_pair(group)
    reply = QuoteReply()
    incoming = dataclasses.replace(
        _incoming(reply, group=group), message_id="m2", auto_reply=True
    )
    config = _config()
    config.quote_reply_enabled = True
    config.quote_race_enabled = True
    config.query_markdown_enabled = False
    try:
        await turn_mod.run_turn(incoming, config=config)
    finally:
        from qq_bot.chat import conversation

        conversation.manager.reset()
    assert reply.quoted == []
    assert any(kind in ("text", "mention") for kind, _ in reply.sent)


async def test_turn_rewrites_alias_to_preferred(patched, repo) -> None:
    from qq_bot.memory.naming import set_my_name

    group = "795245305"
    await repo.upsert_members(
        group, [{"user_id": "U7", "card": "张三", "nickname": "zs"}]
    )
    await set_my_name(repo, user_id="U7", group_id=group, name="狗哥")
    patched.content = "叫张三来"
    reply = FakeReply()
    await turn_mod.run_turn(_incoming(reply, group=group), config=_config())
    assert ("mention", "openid-12345|叫狗哥来") in reply.sent
