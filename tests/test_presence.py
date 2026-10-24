"""群聊存在感 P1：状态机、静默、复读、可接性与编排层。

纯内存 + 注入时钟/随机数，不碰数据库、不 sleep。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from qq_bot.chat import autotrigger, passive_window, presence
from qq_bot.chat.presence import (
    Action,
    PresenceEvent,
    PresenceManager,
    PresenceState,
    RepeatPayload,
    config_from,
)
from qq_bot.platform.ports import Incoming, MediaRef, PlatformCaps

# ── 构造器 ─────────────────────────────────────────────────────────


def cfg(**overrides: object) -> SimpleNamespace:
    """默认参数取自规格 §7；测试只覆盖关心的字段。"""
    base: dict[str, object] = {
        "group_presence_enabled": True,
        "group_presence_rate": 0.2,
        "group_presence_window_sec": 600,
        "group_presence_rate_in_window": 2.0,
        "group_presence_reply_cooldown_sec": 15,
        "group_presence_wake_cooldown_sec": 300,
        "group_presence_daily_active": 120,
        "group_presence_mute_sec": 1800,
        "group_presence_mute_words": "闭嘴,别说了,安静",
        "group_presence_mute_ack": "好",
        "group_presence_ignore_openids": "",
        "group_presence_image_rate_mult": 0.5,
        "group_presence_repeat_enabled": True,
        "group_presence_repeat_min_users": 3,
        "group_presence_repeat_window_sec": 90,
        "group_presence_repeat_max_chars": 20,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def event(  # noqa: PLR0913 —— 构造器字段多
    *,
    group_key: str = "g1",
    user_key: str = "u1",
    text: str = "在吗",
    message_id: str = "",
    has_image: bool = False,
    is_command: bool = False,
    mentions_other: bool = False,
    is_media_only: bool = False,
    repeat_payload: RepeatPayload | None = None,
) -> PresenceEvent:
    return PresenceEvent(
        group_key=group_key,
        user_key=user_key,
        text=text,
        message_id=message_id,
        has_image=has_image,
        is_command=is_command,
        mentions_other=mentions_other,
        is_media_only=is_media_only,
        repeat_payload=repeat_payload,
    )


@pytest.fixture()
def manager() -> PresenceManager:
    return PresenceManager()


# ── 抽奖与窗口 ─────────────────────────────────────────────────────


def test_rate_zero_never_speaks(manager: PresenceManager) -> None:
    decision = manager.observe_active(
        event(), config=config_from(cfg(group_presence_rate=0)), now=0.0, roll=0.0
    )
    assert decision.action is Action.NONE
    assert decision.reason == "rate"


def test_rate_one_always_wakes(manager: PresenceManager) -> None:
    decision = manager.observe_active(
        event(), config=config_from(cfg(group_presence_rate=1)), now=0.0, roll=0.999
    )
    assert decision.action is Action.WAKE
    assert manager.state_of("g1", now=0.0) is PresenceState.AWAKE


def test_wake_sets_window_length(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1))
    manager.observe_active(event(), config=config, now=100.0, roll=0.0)
    group = manager._groups["g1"]
    assert group.expires_at == 700.0


def test_awake_sleeps_when_user_mentions_other(
    manager: PresenceManager,
) -> None:
    config = config_from(cfg(group_presence_rate=1))
    manager.observe_active(event(), config=config, now=0.0, roll=0.0)
    assert manager.state_of("g1", now=0.0) is PresenceState.AWAKE

    decision = manager.observe_active(
        event(message_id="m2", text="@张三 你看这个", mentions_other=True),
        config=config,
        now=10.0,
        roll=0.0,
    )
    assert decision.action is Action.NONE
    assert decision.reason == "mention_other"
    assert manager.state_of("g1", now=10.0) is PresenceState.SLEEPING

    # 退场后进入唤醒冷却，不会立刻又被抽中
    again = manager.observe_active(
        event(message_id="m3"), config=config, now=11.0, roll=0.0
    )
    assert again.reason == "cooldown"


def test_window_reply_cooldown_blocks(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1))
    manager.observe_active(event(), config=config, now=0.0, roll=0.0)
    manager.note_spoken("g1", now=0.0)
    decision = manager.observe_active(event(), config=config, now=5.0, roll=0.0)
    assert decision.action is Action.NONE
    assert decision.reason == "cooldown"


def test_window_speaks_after_cooldown(manager: PresenceManager) -> None:
    config = config_from(
        cfg(group_presence_rate=0.2, group_presence_rate_in_window=2.0)
    )
    manager.observe_active(event(), config=config, now=0.0, roll=0.0)
    manager.note_spoken("g1", now=0.0)
    decision = manager.observe_active(event(), config=config, now=20.0, roll=0.3)
    assert decision.action is Action.IN_WINDOW
    assert decision.threshold == pytest.approx(0.4)


def test_window_timeout_closes_then_wake_cooldown(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1))
    manager.observe_active(event(), config=config, now=0.0, roll=0.0)
    decision = manager.observe_active(event(), config=config, now=601.0, roll=0.0)
    assert decision.reason == "window_timeout"
    assert manager.state_of("g1", now=601.0) is PresenceState.SLEEPING
    # 关窗后 300 秒内不能重新中奖
    decision = manager.observe_active(event(), config=config, now=700.0, roll=0.0)
    assert decision.reason == "cooldown"
    decision = manager.observe_active(event(), config=config, now=901.0, roll=0.0)
    assert decision.action is Action.WAKE


def test_daily_cap_closes_window(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1, group_presence_daily_active=1))
    manager.observe_active(event(), config=config, now=0.0, roll=0.0)
    manager.note_spoken("g1", now=0.0)
    decision = manager.observe_active(event(), config=config, now=20.0, roll=0.0)
    assert decision.reason == "daily_cap"
    assert manager.state_of("g1", now=20.0) is PresenceState.SLEEPING


def test_daily_cap_blocks_sleeping(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1, group_presence_daily_active=0))
    decision = manager.observe_active(event(), config=config, now=0.0, roll=0.0)
    assert decision.reason == "daily_cap"


def test_image_gets_discounted(manager: PresenceManager) -> None:
    config = config_from(
        cfg(group_presence_rate=0.5, group_presence_image_rate_mult=0.5)
    )
    decision = manager.observe_active(
        event(has_image=True, text=""), config=config, now=0.0, roll=0.3
    )
    assert decision.reason == "rate"
    decision = manager.observe_active(
        event(has_image=True, text=""), config=config, now=0.0, roll=0.2
    )
    assert decision.action is Action.WAKE


# ── 可接性 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "/reboot", "is_command": True},
        {"text": "", "is_media_only": True},
        {"text": "看这个", "mentions_other": True},
        {"text": "https://example.com/a"},
    ],
)
def test_unacceptable_messages_never_roll(
    manager: PresenceManager, kwargs: dict[str, object]
) -> None:
    decision = manager.observe_active(
        event(**kwargs),
        config=config_from(cfg(group_presence_rate=1)),
        now=0.0,
        roll=0.0,
    )
    assert decision.action is Action.NONE
    assert decision.reason == "not_acceptable"


def test_duplicate_message_id_ignored(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1))
    manager.observe_active(event(message_id="m1"), config=config, now=0.0, roll=1.0)
    decision = manager.observe_active(
        event(message_id="m1"), config=config, now=0.0, roll=0.0
    )
    assert decision.reason == "duplicate"


def test_ignore_openids_blocks_only_active(manager: PresenceManager) -> None:
    config = config_from(
        cfg(group_presence_rate=1, group_presence_ignore_openids="u1, u2")
    )
    decision = manager.observe_active(
        event(user_key="u1"), config=config, now=0.0, roll=0.0
    )
    assert decision.reason == "ignore"
    decision = manager.observe_passive(
        group_key="g1",
        user_key="u1",
        text="在吗",
        message_id="m1",
        config=config,
        now=0.0,
    )
    assert decision.action is Action.NONE


# ── 静默 ───────────────────────────────────────────────────────────


def test_mute_ack_and_active_blocked(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1))
    decision = manager.observe_passive(
        group_key="g1",
        user_key="u1",
        text="闭嘴",
        message_id="m1",
        config=config,
        now=0.0,
    )
    assert decision.action is Action.MUTE_ACK
    assert decision.ack == "好"
    active = manager.observe_active(
        event(text="在吗"), config=config, now=10.0, roll=0.0
    )
    assert active.reason == "muted"


def test_mute_trigger_ack_does_not_unmute(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1))
    manager.observe_passive(
        group_key="g1",
        user_key="u1",
        text="别说了",
        message_id="m1",
        config=config,
        now=0.0,
    )
    decision = manager.observe_passive(
        group_key="g1",
        user_key="u1",
        text="好",
        message_id="m1",
        config=config,
        now=1.0,
    )
    assert decision.reason == "mute_ack_self"
    assert manager.state_of("g1", now=1.0) is PresenceState.MUTED


def test_other_at_message_unmutes(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1))
    manager.observe_passive(
        group_key="g1",
        user_key="u1",
        text="安静",
        message_id="m1",
        config=config,
        now=0.0,
    )
    decision = manager.observe_passive(
        group_key="g1",
        user_key="u2",
        text="在吗",
        message_id="m2",
        config=config,
        now=5.0,
    )
    assert decision.action is Action.UNMUTE
    assert manager.state_of("g1", now=5.0) is PresenceState.SLEEPING


def test_mute_expires(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_rate=1, group_presence_mute_sec=1800))
    manager.observe_passive(
        group_key="g1",
        user_key="u1",
        text="闭嘴",
        message_id="m1",
        config=config,
        now=0.0,
    )
    decision = manager.observe_active(
        event(text="在吗"), config=config, now=1801.0, roll=0.0
    )
    assert decision.action is Action.WAKE


# ── 复读机 ─────────────────────────────────────────────────────────


def _repeat(text: str, mentions: tuple[str, ...] = ()) -> RepeatPayload:
    parts: list[tuple[str, str]] = []
    if mentions:
        parts.extend(("mention", uid) for uid in mentions)
        parts.append(("text", " " + text))
    else:
        parts.append(("text", text))
    return RepeatPayload(tuple(parts))


def test_repeat_three_distinct_users(manager: PresenceManager) -> None:
    config = config_from(cfg())
    payload = _repeat("难绷")
    for index in range(2):
        decision = manager.observe_active(
            event(user_key=f"u{index}", text="难绷", repeat_payload=payload),
            config=config,
            now=float(index),
            roll=1.0,
        )
        assert decision.action is Action.NONE
    decision = manager.observe_active(
        event(user_key="u2", text="难绷", repeat_payload=payload),
        config=config,
        now=2.0,
        roll=1.0,
    )
    assert decision.action is Action.REPEAT
    assert decision.payload == payload


def test_same_user_spam_never_repeats(manager: PresenceManager) -> None:
    config = config_from(cfg())
    payload = _repeat("难绷")
    for index in range(5):
        decision = manager.observe_active(
            event(user_key="u1", text="难绷", repeat_payload=payload),
            config=config,
            now=float(index),
            roll=1.0,
        )
    assert decision.action is Action.NONE


def test_repeat_key_includes_mentions(manager: PresenceManager) -> None:
    config = config_from(cfg())
    for index, target in enumerate(("u9", "u8", "u7")):
        decision = manager.observe_active(
            event(
                user_key=f"u{index}",
                text="难绷",
                repeat_payload=_repeat("难绷", (target,)),
            ),
            config=config,
            now=float(index),
            roll=1.0,
        )
    # 每句 @ 的人不同，不能算同一句
    assert decision.action is Action.NONE


def test_repeat_same_burst_deduped(manager: PresenceManager) -> None:
    config = config_from(cfg())
    payload = _repeat("难绷")
    for index in range(3):
        manager.observe_active(
            event(user_key=f"u{index}", text="难绷", repeat_payload=payload),
            config=config,
            now=float(index),
            roll=1.0,
        )
    # 复读后再来一个不同用户，同一波不再复读
    decision = manager.observe_active(
        event(user_key="u3", text="难绷", repeat_payload=payload),
        config=config,
        now=10.0,
        roll=1.0,
    )
    assert decision.action is Action.NONE


def test_repeat_counts_toward_daily_not_cooldown(manager: PresenceManager) -> None:
    manager.note_repeat("g1", now=0.0)
    assert manager.daily_count("g1", now=0.0) == 1
    assert manager._groups["g1"].last_spoke_at == 0.0


def test_repeat_disabled(manager: PresenceManager) -> None:
    config = config_from(cfg(group_presence_repeat_enabled=False))
    payload = _repeat("难绷")
    for index in range(3):
        decision = manager.observe_active(
            event(user_key=f"u{index}", text="难绷", repeat_payload=payload),
            config=config,
            now=float(index),
            roll=1.0,
        )
    assert decision.action is Action.NONE


# ── 失败熔断 ───────────────────────────────────────────────────────


def test_no_permission_disables_proactive(manager: PresenceManager) -> None:
    manager.note_failure("g1", code=40034105, now=0.0)
    config = config_from(cfg(group_presence_rate=1))
    decision = manager.observe_active(event(), config=config, now=0.0, roll=0.0)
    assert decision.reason == "proactive_disabled"


def test_network_failure_twice_disables(manager: PresenceManager) -> None:
    manager.note_failure("g1", code=0, now=0.0)
    manager.note_failure("g1", code=0, now=1.0)
    config = config_from(cfg(group_presence_rate=1))
    decision = manager.observe_active(event(), config=config, now=2.0, roll=0.0)
    assert decision.reason == "proactive_disabled"


def test_rate_limited_short_cooldown(manager: PresenceManager) -> None:
    manager.note_failure("g1", code=429, now=0.0)
    config = config_from(cfg(group_presence_rate=1))
    assert (
        manager.observe_active(event(), config=config, now=30.0, roll=0.0).reason
        == "cooldown"
    )
    assert (
        manager.observe_active(event(), config=config, now=61.0, roll=0.0).action
        is Action.WAKE
    )


# ── 被动窗口（回归）───────────────────────────────────────────────


def test_passive_window_register_and_acquire() -> None:
    passive_window.reset()
    reply = object()
    passive_window.register("g1", reply, now=0.0)
    assert passive_window.acquire("g1", now=1.0) is reply
    passive_window.mark_used("g1", now=1.0)
    assert passive_window.acquire("g1", now=2.0) is None


def test_passive_window_expires() -> None:
    passive_window.reset()
    passive_window.register("g1", object(), now=0.0)
    assert passive_window.acquire("g1", now=301.0) is None


def test_passive_window_unknown_group() -> None:
    passive_window.reset()
    assert passive_window.acquire("nope", now=0.0) is None


# ── 编排层 ─────────────────────────────────────────────────────────


@dataclass
class Seg:
    type: str
    data: dict[str, object]


class FakeReply:
    max_parts = 4

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def text(self, content: str) -> None:
        self.texts.append(content)

    async def passive(self, content: str) -> None:
        self.texts.append(content)

    async def mention(self, _user_key: str, content: str) -> None:
        self.texts.append(content)

    async def image(self, _data: bytes, _name: str = "") -> None:
        return None

    async def finish(self) -> None:
        return None


def fake_event(
    *segments: Seg, group_key: str = "g1", bot: bool = False
) -> SimpleNamespace:
    message = list(segments)
    return SimpleNamespace(
        group_openid=group_key,
        author=SimpleNamespace(bot=bot),
        get_message=lambda: message,
    )


def fake_incoming(
    text: str = "在吗",
    *,
    images: list[MediaRef] | None = None,
    message_id: str = "m1",
    user_key: str = "u1",
    group_key: str = "g1",
) -> Incoming:
    return Incoming(
        user_key=user_key,
        group_key=group_key,
        display_name="小明",
        text=text,
        images=images or [],
        message_id=message_id,
        reply=FakeReply(),
        caps=PlatformCaps(
            can_see_all_group_messages=True,
            can_send_image=True,
            max_reply_parts=4,
        ),
    )


@pytest.fixture(autouse=True)
def _clean_manager() -> None:
    presence.manager.reset()
    passive_window.reset()


@pytest.mark.asyncio
async def test_autotrigger_speaks_with_active_message(monkeypatch) -> None:
    sent: list[str] = []

    async def fake_run_turn(incoming, config):  # noqa: ARG001
        await incoming.reply.text("在")

    monkeypatch.setattr(autotrigger, "run_turn", fake_run_turn)
    ok = await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "在吗"})),
        config=cfg(group_presence_rate=1),
        incoming=fake_incoming("在吗"),
        now=0.0,
        roll=0.0,
        send_text=_collect(sent),
    )
    assert ok is True
    assert sent == ["在"]
    assert presence.manager.state_of("g1", now=0.0) is PresenceState.AWAKE


def _collect(sink: list[str]):
    async def send(content: str) -> None:
        sink.append(content)

    return send


@pytest.mark.asyncio
async def test_autotrigger_repeat_preserves_mentions() -> None:
    captured: list[RepeatPayload] = []

    async def fake_send(payload: RepeatPayload) -> None:
        captured.append(payload)

    segments = [
        Seg("mention_user", {"user_id": "u9"}),
        Seg("text", {"text": " 难绷"}),
    ]
    config = cfg()
    for index in range(3):
        ok = await autotrigger.maybe_autotrigger(
            object(),
            fake_event(*segments),
            config=config,
            incoming=fake_incoming(
                "难绷", user_key=f"u{index}", message_id=f"m{index}"
            ),
            now=float(index),
            roll=1.0,
            send_repeat=fake_send,
        )
    assert ok is True
    assert captured and captured[0].parts == (
        ("mention", "u9"),
        ("text", " 难绷"),
    )
    assert captured[0].mentions == ("u9",)


@pytest.mark.asyncio
async def test_autotrigger_failure_disables_group(monkeypatch) -> None:
    class BoomError(Exception):
        code = 40034105

    async def fake_run_turn(incoming, config):  # noqa: ARG001
        raise BoomError

    monkeypatch.setattr(autotrigger, "run_turn", fake_run_turn)
    ok = await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "在吗"})),
        config=cfg(group_presence_rate=1),
        incoming=fake_incoming("在吗"),
        now=0.0,
        roll=0.0,
        send_text=_collect([]),
    )
    assert ok is False
    assert presence.manager._groups["g1"].active_ok is False


@pytest.mark.asyncio
async def test_autotrigger_skips_bot_and_missing_incoming() -> None:
    assert (
        await autotrigger.maybe_autotrigger(
            object(),
            fake_event(bot=True),
            config=cfg(),
            incoming=fake_incoming(),
            now=0.0,
        )
        is False
    )
    assert (
        await autotrigger.maybe_autotrigger(
            object(), fake_event(), config=cfg(), incoming=None, now=0.0
        )
        is False
    )


@pytest.mark.asyncio
async def test_autotrigger_swallows_exceptions(monkeypatch) -> None:
    class DbError(Exception):
        pass

    def boom(_settings):
        raise DbError

    monkeypatch.setattr(presence, "config_from", boom)
    ok = await autotrigger.maybe_autotrigger(
        object(), fake_event(), config=cfg(), incoming=fake_incoming(), now=0.0
    )
    assert ok is False


# ── P2：关键词触发 ────────────────────────────────────────────────


def keyword_config(**overrides: object) -> presence.PresenceConfig:
    return config_from(cfg(**overrides))


def test_observe_keyword_call_answers_once(manager: PresenceManager) -> None:
    decision = manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="call",
        message_id="k1",
        config=keyword_config(),
        now=0.0,
    )
    assert decision.action is Action.KEYWORD_CALL
    assert manager.state_of("g1", now=0.0) is PresenceState.SLEEPING
    # 叫一声只回一次；后面的消息不会因为这一声自动变成窗口内接话
    nxt = manager.observe_active(
        event(text="在吗", message_id="k2"),
        config=keyword_config(),
        now=5.0,
        roll=0.99,
    )
    assert nxt.reason == "rate"


def test_observe_keyword_about_sets_hook(manager: PresenceManager) -> None:
    decision = manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="about",
        message_id="k1",
        config=keyword_config(),
        now=0.0,
    )
    assert decision.action is Action.KEYWORD_ABOUT
    hooked = manager.observe_active(
        event(text="在聊什么呢", message_id="k2"),
        config=keyword_config(),
        now=10.0,
        roll=0.99,
    )
    assert hooked.action is Action.HOOK
    again = manager.observe_active(
        event(text="继续", message_id="k3"),
        config=keyword_config(),
        now=11.0,
        roll=0.99,
    )
    assert again.action is not Action.HOOK


def test_observe_keyword_ignored_inside_window(manager: PresenceManager) -> None:
    manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="about",
        message_id="k1",
        config=keyword_config(),
        now=0.0,
    )
    decision = manager.observe_keyword(
        group_key="g1",
        user_key="u2",
        kind="call",
        message_id="k2",
        config=keyword_config(),
        now=5.0,
    )
    assert decision.action is Action.NONE
    assert decision.reason == "keyword_awake"


def test_observe_keyword_group_cooldown(manager: PresenceManager) -> None:
    config = keyword_config(
        group_presence_window_sec=0, group_presence_keyword_cooldown_sec=120
    )
    manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="call",
        message_id="k1",
        config=config,
        now=0.0,
    )
    manager.observe_active(
        event(text="hi", message_id="k2"), config=config, now=1.0, roll=0.99
    )
    decision = manager.observe_keyword(
        group_key="g1",
        user_key="u2",
        kind="call",
        message_id="k3",
        config=config,
        now=2.0,
    )
    assert decision.reason == "keyword_cooldown"


def test_observe_keyword_user_cooldown(manager: PresenceManager) -> None:
    config = keyword_config(
        group_presence_window_sec=0,
        group_presence_keyword_cooldown_sec=0,
        group_presence_keyword_user_cooldown_sec=300,
    )
    manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="call",
        message_id="k1",
        config=config,
        now=0.0,
    )
    manager.observe_active(
        event(text="hi", message_id="k2"), config=config, now=1.0, roll=0.99
    )
    decision = manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="call",
        message_id="k3",
        config=config,
        now=2.0,
    )
    assert decision.reason == "keyword_user_cooldown"


def test_observe_keyword_homonym_skips(manager: PresenceManager) -> None:
    decision = manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="homonym",
        message_id="k1",
        config=keyword_config(),
        now=0.0,
    )
    assert decision.action is Action.NONE
    assert decision.reason == "homonym"
    assert manager.state_of("g1", now=0.0) is PresenceState.SLEEPING


def test_observe_keyword_respects_mute_blacklist_daily(
    manager: PresenceManager,
) -> None:
    muted = keyword_config()
    manager.observe_passive(
        group_key="g1",
        user_key="u1",
        text="闭嘴",
        message_id="m0",
        config=muted,
        now=0.0,
    )
    decision = manager.observe_keyword(
        group_key="g1",
        user_key="u1",
        kind="call",
        message_id="k1",
        config=muted,
        now=1.0,
    )
    assert decision.reason == "muted"

    blocked = keyword_config(group_presence_ignore_openids="u9")
    decision = manager.observe_keyword(
        group_key="g2",
        user_key="u9",
        kind="call",
        message_id="k2",
        config=blocked,
        now=1.0,
    )
    assert decision.reason == "ignore"

    capped = keyword_config(group_presence_daily_active=0)
    decision = manager.observe_keyword(
        group_key="g3",
        user_key="u1",
        kind="call",
        message_id="k3",
        config=capped,
        now=1.0,
    )
    assert decision.reason == "daily_cap"


@pytest.mark.asyncio
async def test_autotrigger_keyword_call_runs_turn_without_hint(monkeypatch) -> None:
    seen: list[str] = []

    async def fake_run_turn(incoming, config):  # noqa: ARG001
        seen.append(incoming.text)
        await incoming.reply.text("在")

    async def fake_classify(_text: str, _keyword: str) -> str:
        return "call"

    monkeypatch.setattr(autotrigger, "run_turn", fake_run_turn)
    ok = await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "大肥鱼 在吗"})),
        config=cfg(),
        incoming=fake_incoming("大肥鱼 在吗"),
        now=0.0,
        classify=fake_classify,
        send_text=_collect([]),
    )
    assert ok is True
    assert seen == ["大肥鱼 在吗"]


@pytest.mark.asyncio
async def test_autotrigger_keyword_about_sends_transition() -> None:
    sent: list[str] = []

    async def fake_classify(_text: str, _keyword: str) -> str:
        return "about"

    async def fake_generate(**_kwargs: object) -> str:
        return "哼哼 我听到有人在背后夸我"

    ok = await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "我觉得我的大肥鱼更牛逼"})),
        config=cfg(),
        incoming=fake_incoming("我觉得我的大肥鱼更牛逼"),
        now=0.0,
        classify=fake_classify,
        generate=fake_generate,
        send_text=_collect(sent),
    )
    assert ok is True
    assert sent == ["哼哼 我听到有人在背后夸我"]


@pytest.mark.asyncio
async def test_autotrigger_keyword_falls_back_to_fixed_sentence() -> None:
    sent: list[str] = []

    async def fake_classify(_text: str, _keyword: str) -> str:
        return "about"

    async def fake_generate(**_kwargs: object) -> None:
        return None

    ok = await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "今天大肥鱼没说话"})),
        config=cfg(),
        incoming=fake_incoming("今天大肥鱼没说话"),
        now=0.0,
        classify=fake_classify,
        generate=fake_generate,
        send_text=_collect(sent),
    )
    assert ok is True
    assert sent == ["什么意思"]


@pytest.mark.asyncio
async def test_autotrigger_keyword_homonym_sends_nothing() -> None:
    sent: list[str] = []

    async def fake_classify(_text: str, _keyword: str) -> str:
        return "homonym"

    ok = await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "大肥鱼丸好吃吗"})),
        config=cfg(),
        incoming=fake_incoming("大肥鱼丸好吃吗"),
        now=0.0,
        classify=fake_classify,
        send_text=_collect(sent),
    )
    assert ok is False
    assert sent == []


# ── P3：窗口内模型判定 ───────────────────────────────────────────


def test_pass_limit_closes_window(manager: PresenceManager) -> None:
    config = keyword_config(group_presence_rate=1, group_presence_pass_limit=2)
    wake = manager.observe_active(
        event(text="在吗", message_id="a1"), config=config, now=0.0, roll=0.0
    )
    assert wake.action is Action.WAKE
    assert manager.state_of("g1", now=0.0) is PresenceState.AWAKE

    assert manager.note_pass("g1", config=config, now=1.0) is False
    assert manager.state_of("g1", now=1.0) is PresenceState.AWAKE
    assert manager.note_pass("g1", config=config, now=2.0) is True
    assert manager.state_of("g1", now=2.0) is PresenceState.SLEEPING


def test_force_reply_ignores_rate(manager: PresenceManager) -> None:
    config = keyword_config(group_presence_rate=1)
    manager.observe_active(
        event(text="在吗", message_id="b1"), config=config, now=0.0, roll=0.0
    )
    quiet = keyword_config(group_presence_rate=0.0, group_presence_reply_cooldown_sec=0)
    decision = manager.observe_active(
        event(text="聊点别的", message_id="b2"),
        config=quiet,
        now=1.0,
        roll=0.99,
        force=True,
    )
    assert decision.action is Action.IN_WINDOW
    plain = manager.observe_active(
        event(text="还是聊点别的", message_id="b3"),
        config=quiet,
        now=2.0,
        roll=0.99,
    )
    assert plain.action is Action.NONE


def test_close_window_reason_model_left(manager: PresenceManager) -> None:
    config = keyword_config(group_presence_rate=1)
    manager.observe_active(
        event(text="在吗", message_id="c1"), config=config, now=0.0, roll=0.0
    )
    manager.close_window("g1", reason="model_left", config=config, now=3.0)
    assert manager.state_of("g1", now=3.0) is PresenceState.SLEEPING


@pytest.mark.asyncio
async def test_autotrigger_judge_pass_keeps_silent(monkeypatch) -> None:
    sent: list[str] = []

    async def fake_run_turn(incoming, config):  # noqa: ARG001
        await incoming.reply.text("在")

    monkeypatch.setattr(autotrigger, "run_turn", fake_run_turn)
    judge = _fake_judge("pass")
    config = cfg(group_presence_rate=1, group_presence_judge_enabled=True)
    first = await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "在吗"})),
        config=config,
        incoming=fake_incoming("在吗", message_id="j1"),
        now=0.0,
        roll=0.0,
        send_text=_collect(sent),
        judge=judge,
    )
    assert first is True
    await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "他们聊他们的"})),
        config=config,
        incoming=fake_incoming("他们聊他们的", message_id="j2"),
        now=1.0,
        roll=0.99,
        send_text=_collect(sent),
        judge=judge,
    )
    assert sent == ["在"]
    assert presence.manager.state_of("g1", now=1.0) is PresenceState.AWAKE


@pytest.mark.asyncio
async def test_autotrigger_judge_sleep_closes_window(monkeypatch) -> None:
    sent: list[str] = []

    async def fake_run_turn(incoming, config):  # noqa: ARG001
        await incoming.reply.text("在")

    monkeypatch.setattr(autotrigger, "run_turn", fake_run_turn)
    config = cfg(group_presence_rate=1, group_presence_judge_enabled=True)
    await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "在吗"})),
        config=config,
        incoming=fake_incoming("在吗", message_id="s1"),
        now=0.0,
        roll=0.0,
        send_text=_collect(sent),
        judge=_fake_judge("sleep"),
    )
    await autotrigger.maybe_autotrigger(
        object(),
        fake_event(Seg("text", {"text": "他们聊他们的"})),
        config=config,
        incoming=fake_incoming("他们聊他们的", message_id="s2"),
        now=1.0,
        roll=0.99,
        send_text=_collect(sent),
        judge=_fake_judge("sleep"),
    )
    assert sent == ["在"]
    assert presence.manager.state_of("g1", now=1.0) is PresenceState.SLEEPING


def _fake_judge(action: str):
    from qq_bot.chat.judge import JudgeAction

    async def fake(**_kwargs: object) -> JudgeAction:
        return JudgeAction(action)

    return fake
