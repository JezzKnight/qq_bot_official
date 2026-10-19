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
        platform="qq",
        user_key="openid-12345",
        group_key=group,
        display_name="测试用户",
        text="你好",
        images=[],
        message_id="1",
        reply=reply,
        caps=QQ_CAPS,
        self_key="999",
        card="",
        nickname="测试用户",
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


async def patched_repo_rows():
    # 借助 turn_mod.get_memory 的返回值拿仓储（fixture 已把它指向 repo）
    repo = await turn_mod.get_memory()
    return await repo.pool.fetch(
        "SELECT role, content FROM messages ORDER BY id"
    )


def test_compression_skipped_without_full_group_access() -> None:
    incoming = dataclasses.replace(
        _incoming(FakeReply()),
        caps=PlatformCaps(
            can_list_members=False,
            can_see_all_group_messages=False,
            can_send_image=True,
            can_active_message=False,
            max_reply_parts=4,
            can_progress_messages=False,
        ),
    )
    assert turn_mod._compression_scheduler(incoming) is None


def test_compression_enabled_with_full_group_access() -> None:
    incoming = dataclasses.replace(
        _incoming(FakeReply()),
        caps=PlatformCaps(
            can_list_members=False,
            can_see_all_group_messages=True,
            can_send_image=True,
            can_active_message=False,
            max_reply_parts=4,
            can_progress_messages=False,
        ),
    )
    assert turn_mod._compression_scheduler(incoming) is not None
