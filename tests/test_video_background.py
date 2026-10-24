"""后台视频任务：主 turn 立即返回，分析完成后单独回复并落库。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qq_bot.chat import turn as turn_mod
from qq_bot.chat import video_background
from qq_bot.chat.persist import persist_user
from qq_bot.platform.ports import Incoming, MediaRef, PlatformCaps


class FakeReply:
    max_parts = 4

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


def _incoming(reply: FakeReply) -> Incoming:
    return Incoming(
        user_key="openid-12345",
        group_key=None,
        display_name="测试用户",
        text="看看这个视频",
        images=[
            MediaRef(
                kind="video",
                url="https://www.bilibili.com/video/BV1xGnw6vEeF",
            )
        ],
        message_id="msg-1",
        reply=reply,
        caps=PlatformCaps(
            can_see_all_group_messages=False,
            can_send_image=True,
            max_reply_parts=4,
        ),
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        memory_injection_enabled=False,
        glossary_enabled=False,
        max_context_tokens=4000,
        video_background_enabled=True,
        reply_typing_delay=False,
        bot_self_id="999",
    )


def _async_value(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner()


@pytest.fixture
def patched_turn(monkeypatch, repo):
    async def _no_llm(_model):
        raise AssertionError("后台视频分支不应调用主模型")

    monkeypatch.setattr(turn_mod, "get_memory", lambda: _async_value(repo))
    monkeypatch.setattr(turn_mod, "get_client_for_model", _no_llm)
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
    monkeypatch.setattr(turn_mod, "schedule_compress", lambda _gid: None)
    return repo


async def test_video_turn_returns_without_calling_model(
    patched_turn, monkeypatch
) -> None:
    jobs: list[video_background.VideoJob] = []

    def fake_start(job: video_background.VideoJob) -> bool:
        jobs.append(job)
        return True

    monkeypatch.setattr(turn_mod, "start_video_job", fake_start)
    monkeypatch.setattr(turn_mod, "is_video_job_active", lambda *_args: False)

    reply = FakeReply()
    result = await turn_mod.run_turn(_incoming(reply), config=_config())

    assert result == ""
    assert reply.sent == [("passive", turn_mod.VIDEO_ACK)]
    assert len(jobs) == 1
    assert jobs[0].source.url == "https://www.bilibili.com/video/BV1xGnw6vEeF"

    rows = await patched_turn.pool.fetch(
        "SELECT role, content FROM messages ORDER BY id"
    )
    assert [row["role"] for row in rows] == ["user"]
    assert "看看这个视频" in rows[0]["content"]


async def test_background_job_answers_and_persists_note(repo, monkeypatch) -> None:
    class FakeAgent:
        def __init__(self, **_kwargs) -> None:
            pass

        async def analyze(self, _source, *, question: str):
            assert question == "说说这个视频"
            return SimpleNamespace(text="视频里在讲暗猫的角色评测")

    class FakeClient:
        async def chat(self, **_kwargs):
            return SimpleNamespace(
                content="暗猫很强 视频里说值得抽",
                prompt_tokens=100,
                cached_tokens=0,
                completion_tokens=20,
                reasoning_content=None,
            )

    monkeypatch.setattr(video_background, "VideoAgent", FakeAgent)

    async def _get_client(_model):
        return FakeClient()

    monkeypatch.setattr(video_background, "get_client_for_model", _get_client)

    thread_id = await repo.ensure_thread("private", None, "openid-12345")
    await persist_user(
        repo,
        thread_id=thread_id,
        group_id=None,
        sender_id="openid-12345",
        sender_name="测试用户",
        user_text='<user identity id="openid-12345" name="测试用户"/>\n看看这个视频',
    )

    reply = FakeReply()
    incoming = Incoming(
        user_key="openid-12345",
        group_key=None,
        display_name="测试用户",
        text="说说这个视频",
        images=[],
        message_id="msg-1",
        reply=reply,
        caps=PlatformCaps(
            can_see_all_group_messages=False,
            can_send_image=True,
            max_reply_parts=4,
        ),
    )
    job = video_background.VideoJob(
        incoming=incoming,
        thread_id=thread_id,
        group_id=None,
        sender_id="openid-12345",
        sender_name="测试用户",
        user_text='<user identity id="openid-12345" name="测试用户"/>\n看看这个视频',
        question="说说这个视频",
        source=MediaRef(
            kind="video", url="https://www.bilibili.com/video/BV1xGnw6vEeF"
        ),
        system_prompt="你是测试机器人",
        history=[],
        model_name="test-model",
        temperature=0.7,
        max_tokens=100,
        max_context_tokens=4000,
        memory=repo,
        schedule=None,
    )

    await video_background._run_video_job(job)

    assert reply.sent == [("text", "暗猫很强 视频里说值得抽")]
    assert reply.finished is True
    rows = await repo.pool.fetch(
        "SELECT role, content FROM messages WHERE thread_id=$1 ORDER BY id",
        thread_id,
    )
    assert [row["role"] for row in rows] == ["user", "assistant", "assistant"]
    assert rows[1]["content"] == "暗猫很强 视频里说值得抽"
    assert "视频里在讲暗猫的角色评测" in rows[2]["content"]
