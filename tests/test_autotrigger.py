"""群聊自动接话 L1 的测试：判定纯函数 / 被动窗口 / 编排层 三层分开测。

编排层用 monkeypatch 替换 ``qq_bot.chat.autotrigger.run_turn``，全程不碰模型、
不连数据库；时钟全部显式注入，测试里没有 sleep。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from qq_bot.chat import activation, autotrigger, passive_window
from qq_bot.platform.ports import Incoming, PlatformCaps

if TYPE_CHECKING:
    from collections.abc import Iterator


class FakeReply:
    """记录所有发送动作的 ReplyTarget 替身。"""

    max_parts = 4

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def text(self, content: str) -> None:
        self.sent.append(("text", content))

    async def passive(self, content: str) -> None:
        self.sent.append(("passive", content))

    async def mention(self, _user_key: str, content: str) -> None:
        self.sent.append(("mention", content))

    async def image(self, _data: bytes, name: str = "") -> None:
        self.sent.append(("image", name))

    async def finish(self) -> None:
        self.sent.append(("finish", ""))


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    passive_window.reset()
    autotrigger.reset_state()
    yield
    autotrigger.reset_state()
    passive_window.reset()


def _config(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "group_autotrigger": True,
        "group_autotrigger_daily_cap": 6,
        "group_autotrigger_cooldown_sec": 90,
        "group_autotrigger_probability": 0.0,
        "group_autotrigger_min_score": 4,
        "group_autotrigger_window_seconds": 300,
        "group_autotrigger_names": "大肥鱼,肥鱼,鱼鱼",
        "group_autotrigger_hint": "（顺嘴接）",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _incoming(
    text: str = "今天好热",
    *,
    user_key: str = "u1",
    group_key: str = "g1",
    images: list[object] | None = None,
    reply: FakeReply | None = None,
) -> Incoming:
    return Incoming(
        user_key=user_key,
        group_key=group_key,
        display_name="小明",
        text=text,
        images=images or [],  # type: ignore[arg-type]
        message_id="m1",
        reply=reply or FakeReply(),  # type: ignore[arg-type]
        caps=PlatformCaps(
            can_see_all_group_messages=True,
            can_send_image=True,
            max_reply_parts=4,
        ),
    )


def _event(
    *, group: str = "g1", bot_author: bool = False, reply: object | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        group_openid=group,
        author=SimpleNamespace(bot=bot_author, member_openid="u1"),
        reply=reply,
    )


def _bot() -> SimpleNamespace:
    """占位 bot：探测接口不存在时编排层应静默跳过。"""
    return SimpleNamespace()


def _decide(text: str = "今天好热", **overrides: object) -> activation.Decision:
    kwargs: dict[str, object] = {
        "is_command": False,
        "has_media": False,
        "quoted_someone": False,
        "mentions_bot_name": False,
        "is_question": False,
        "has_second_person": False,
        "overlaps_last_bot_msg": False,
        "minutes_since_bot_spoke": None,
        "same_sender_streak": 0,
        "cooldown_ok": True,
        "daily_budget_ok": True,
        "window_available": True,
        "probability": 1.0,
        "min_score": 4,
    }
    kwargs.update(overrides)
    return activation.decide(text, **kwargs)  # type: ignore[arg-type]


class _RunTurnSpy:
    """替代 run_turn：记录每次调用，可配置抛异常。"""

    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[Incoming] = []
        self._raise = raise_error

    async def __call__(
        self,
        incoming: Incoming,
        *,
        config: object,  # noqa: ARG002 — 签名须与 run_turn 的 config= 一致
    ) -> str:
        if self._raise:
            raise RuntimeError("model down")  # noqa: TRY003
        self.calls.append(incoming)
        return "嗯"


# ── 判定层 ───────────────────────────────────────────────────────────────


def test_veto_command() -> None:
    decision = _decide("今天好热", is_command=True)
    assert not decision.allowed
    assert "command" in decision.reason


def test_veto_blank_text() -> None:
    decision = _decide("   ")
    assert not decision.allowed
    assert "empty" in decision.reason


def test_veto_media_only() -> None:
    decision = _decide("", has_media=True)
    assert not decision.allowed
    assert "media_only" in decision.reason


def test_veto_sender_streak() -> None:
    # 先保证其它维度能过（名字 + 问句 = 4 分），只让 streak 这一条否决
    kwargs = {"mentions_bot_name": True, "is_question": True}
    assert _decide(same_sender_streak=2, **kwargs).allowed
    decision = _decide(same_sender_streak=3, **kwargs)
    assert not decision.allowed
    assert "sender_streak" in decision.reason


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"cooldown_ok": False}, "cooldown"),
        ({"daily_budget_ok": False}, "daily_budget"),
        ({"window_available": False}, "no_window"),
    ],
)
def test_veto_state_flags(override: dict[str, object], needle: str) -> None:
    decision = _decide(**override)
    assert not decision.allowed
    assert needle in decision.reason


def test_score_stacks_quote_name_question() -> None:
    decision = _decide(
        "大肥鱼 你觉得呢",
        quoted_someone=True,
        mentions_bot_name=True,
        is_question=True,
    )
    assert decision.score == 7
    assert decision.allowed


def test_probability_zero_never_passes() -> None:
    decision = _decide(
        "大肥鱼 吗", mentions_bot_name=True, is_question=True, probability=0.0
    )
    assert not decision.allowed
    assert "probability" in decision.reason


def test_probability_one_always_passes() -> None:
    decision = _decide(
        "大肥鱼 吗", mentions_bot_name=True, is_question=True, probability=1.0
    )
    assert decision.allowed


def test_min_score_boundary() -> None:
    passed = _decide(
        "大肥鱼 吗", mentions_bot_name=True, is_question=True, min_score=4
    )
    assert passed.score == 4
    assert passed.allowed
    rejected = _decide(
        "大肥鱼 吗", mentions_bot_name=True, is_question=True, min_score=5
    )
    assert not rejected.allowed
    assert "below min" in rejected.reason


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(None, 0), (5.0, 0), (10.0, 0), (11.0, 1)],
)
def test_long_silence_bonus(minutes: float | None, expected: int) -> None:
    assert _decide(minutes_since_bot_spoke=minutes).score == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你吃了吗？", True),
        ("为啥不理我", True),
        ("能不能说句话", True),
        ("今天好热", False),
    ],
)
def test_looks_like_question(text: str, expected: bool) -> None:  # noqa: FBT001
    assert activation.looks_like_question(text) is expected


# ── 被动窗口登记表 ───────────────────────────────────────────────────────


def test_window_register_then_acquire() -> None:
    reply = FakeReply()
    passive_window.register("g1", reply, now=100.0)
    assert passive_window.acquire("g1", now=100.0) is reply


def test_window_expires() -> None:
    passive_window.register("g1", FakeReply(), now=100.0)
    assert passive_window.acquire("g1", now=399.0) is not None
    assert passive_window.acquire("g1", now=401.0) is None


def test_window_used_limit() -> None:
    passive_window.register("g1", FakeReply(), used=0, now=100.0)
    for _ in range(4):
        passive_window.mark_used("g1", now=101.0)
    assert passive_window.acquire("g1", now=102.0) is not None
    passive_window.mark_used("g1", now=103.0)
    assert passive_window.acquire("g1", now=104.0) is None


def test_window_default_reserves_only_one_reply() -> None:
    passive_window.register("g1", FakeReply(), now=100.0)  # 默认按 4 段已用记账
    assert passive_window.acquire("g1", now=100.0) is not None
    passive_window.mark_used("g1", now=100.0)
    assert passive_window.acquire("g1", now=100.0) is None


def test_window_expired_is_cleaned_and_capped() -> None:
    for i in range(passive_window.MAX_WINDOWS + 50):
        passive_window.register(f"g{i}", FakeReply(), now=1000.0)
    assert len(passive_window._windows) <= passive_window.MAX_WINDOWS
    passive_window.clear_expired(now=2000.0)
    assert passive_window.acquire("g0", now=2000.0) is None


def test_minutes_since_bot_spoke() -> None:
    assert passive_window.minutes_since_bot_spoke("g1", now=100.0) is None
    passive_window.register("g1", FakeReply(), now=100.0)
    assert passive_window.minutes_since_bot_spoke("g1", now=400.0) == 5.0


# ── 编排层 ───────────────────────────────────────────────────────────────


async def test_no_window_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(), incoming=_incoming("大肥鱼 你觉得呢？"), config=_config(), now=1000.0
    )
    assert spy.calls == []


async def test_window_and_score_triggers_single_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    passive_window.register("g1", FakeReply(), now=1000.0)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(),
        incoming=_incoming("大肥鱼 你觉得呢？"),
        config=_config(group_autotrigger_probability=1.0),
        now=1000.0,
    )
    assert len(spy.calls) == 1
    auto = spy.calls[0]
    assert isinstance(auto.reply, autotrigger.WindowReply)
    assert auto.reply.max_parts == 1
    assert auto.text.startswith("（顺嘴接）")
    # 一次接话就把窗口的最后一个名额用掉了
    assert passive_window.acquire("g1", now=1000.0) is None


async def test_daily_cap_sixth_passes_seventh_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    config = _config(
        group_autotrigger_probability=1.0, group_autotrigger_cooldown_sec=0
    )
    for i in range(7):
        clock = 1000.0 + i * 10
        passive_window.register("g1", FakeReply(), now=clock)
        await autotrigger.maybe_autotrigger(
            _bot(),
            _event(),
            incoming=_incoming(f"大肥鱼 第{i}句？", user_key=f"u{i}"),
            config=config,
            now=clock,
        )
    assert len(spy.calls) == 6


async def test_cooldown_blocks_second(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    config = _config(group_autotrigger_probability=1.0)
    passive_window.register("g1", FakeReply(), now=1000.0)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(), incoming=_incoming("大肥鱼 第一句？"), config=config, now=1000.0
    )
    passive_window.register("g1", FakeReply(), now=1050.0)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(), incoming=_incoming("大肥鱼 第二句？"), config=config, now=1050.0
    )
    assert len(spy.calls) == 1


async def test_bot_self_and_command_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    config = _config(group_autotrigger_probability=1.0)
    passive_window.register("g1", FakeReply(), now=1000.0)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(bot_author=True),
        incoming=_incoming("大肥鱼 你觉得呢？"),
        config=config,
        now=1000.0,
    )
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(), incoming=_incoming("/help"), config=config, now=1000.0
    )
    assert spy.calls == []


async def test_media_only_and_blank_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    config = _config(group_autotrigger_probability=1.0)
    passive_window.register("g1", FakeReply(), now=1000.0)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(), incoming=_incoming(""), config=config, now=1000.0
    )
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(),
        incoming=_incoming("", images=[object()]),
        config=config,
        now=1000.0,
    )
    assert spy.calls == []


async def test_hint_text_does_not_add_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """hint 里的「你」不能给消息加分：判定只看用户原话。

    hint（「（这没人在叫你，你只是顺嘴插一句）」）是拼给模型看的，里面带第二人称；
    旧实现拿拼好的 turn_text 算信号，于是每条消息都白得 1 分，阈值 4 实际变成 3。
    """
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    passive_window.register("g1", FakeReply(), now=1000.0)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(),
        incoming=_incoming("今天 35 度"),
        config=_config(
            group_autotrigger_probability=1.0,
            group_autotrigger_min_score=1,
            group_autotrigger_hint="（这没人在叫你，你只是顺嘴插一句）",
        ),
        now=1000.0,
    )
    assert spy.calls == []


async def test_media_only_is_vetoed_even_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯图片消息：hint 让拼好的文本非空，但否决必须仍然命中 media_only。"""
    spy = _RunTurnSpy()
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    passive_window.register("g1", FakeReply(), now=1000.0)
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(),
        incoming=_incoming("", images=[object()]),
        config=_config(
            group_autotrigger_probability=1.0,
            group_autotrigger_min_score=0,
            group_autotrigger_hint="（这没人在叫你，你只是顺嘴插一句）",
        ),
        now=1000.0,
    )
    assert spy.calls == []


async def test_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _RunTurnSpy(raise_error=True)
    monkeypatch.setattr(autotrigger, "run_turn", spy)
    reply = FakeReply()
    passive_window.register("g1", reply, now=1000.0)
    # 不抛异常、不发任何错误提示
    await autotrigger.maybe_autotrigger(
        _bot(),
        _event(),
        incoming=_incoming("大肥鱼 你觉得呢？"),
        config=_config(group_autotrigger_probability=1.0),
        now=1000.0,
    )
    assert reply.sent == []
    assert passive_window.acquire("g1", now=1000.0) is reply  # 名额没被记走


async def test_window_reply_downgrades_mention_and_skips_image() -> None:
    reply = FakeReply()
    wrapper = autotrigger.WindowReply(reply)
    await wrapper.mention("u9", "在的")
    await wrapper.image(b"png")
    await wrapper.finish()
    assert wrapper.max_parts == 1
    assert reply.sent == [("passive", "在的"), ("finish", "")]


async def test_probe_group_state_caches_result() -> None:
    class Bot:
        calls = 0

        async def get_group_bot_state(
            self,
            *,
            group_id: str,  # noqa: ARG002 — 与真实 API 的 group_id= 对齐
        ) -> SimpleNamespace:
            Bot.calls += 1
            return SimpleNamespace(
                recv_msg_setting="all",
                allow_proactive_msg=True,
                member_role="member",
            )

    bot = Bot()
    assert await autotrigger.probe_group_state(bot, "g1", now=1000.0) is not None
    assert await autotrigger.probe_group_state(bot, "g1", now=1100.0) is not None
    assert Bot.calls == 1


async def test_probe_group_state_swallows_errors() -> None:
    class Bot:
        async def get_group_bot_state(
            self,
            *,
            group_id: str,  # noqa: ARG002 — 与真实 API 的 group_id= 对齐
        ) -> None:
            raise RuntimeError("boom")

    assert await autotrigger.probe_group_state(Bot(), "g2", now=1000.0) is None
