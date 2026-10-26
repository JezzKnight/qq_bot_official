"""AWAKE 下的 5 秒防抖：同人重置、他人并入、拼接文本判定。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from qq_bot.chat import autotrigger, conversation, presence
from qq_bot.chat.presence import PresenceState, config_from
from qq_bot.platform.ports import Incoming, PlatformCaps

DEBOUNCE = 0.2


def cfg(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "group_presence_enabled": True,
        "group_presence_rate": 0.2,
        "group_presence_window_sec": 600,
        "group_presence_rate_in_window": 2.0,
        "group_presence_active_grace_sec": 180,
        "group_presence_debounce_sec": DEBOUNCE,
        "group_presence_reply_cooldown_sec": 0,
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
        "group_presence_judge_enabled": False,
        "group_presence_policy_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def fake_event(group_key: str = "g1", text: str = "在吗") -> SimpleNamespace:
    segments = [SimpleNamespace(type="text", data={"text": text})]
    return SimpleNamespace(
        group_openid=group_key,
        author=SimpleNamespace(bot=False),
        get_message=lambda: segments,
    )


def incoming(
    text: str,
    *,
    user_key: str = "u1",
    message_id: str = "",
    group_key: str = "g1",
) -> Incoming:
    return Incoming(
        user_key=user_key,
        group_key=group_key,
        display_name="小明",
        text=text,
        images=[],
        message_id=message_id or f"m-{text}",
        reply=SimpleNamespace(),
        caps=PlatformCaps(
            can_see_all_group_messages=True,
            can_send_image=True,
            max_reply_parts=4,
        ),
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    autotrigger.reset_state()
    conversation.manager.reset()
    yield
    autotrigger.reset_state()
    conversation.manager.reset()


def awake(settings: SimpleNamespace, group_key: str = "g1") -> None:
    presence.manager.open_window(
        group_key,
        config=config_from(settings),
        now=time.monotonic(),
        reason="test",
    )


async def test_merges_fragments_into_one_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = cfg()
    awake(settings)
    calls: list[str] = []

    async def fake_turn(inc: Incoming, **_: object) -> str:
        calls.append(inc.text)
        return ""

    monkeypatch.setattr(autotrigger, "run_turn", fake_turn)
    bot = SimpleNamespace()

    for text in ("你", "明天", "几点起床"):
        await autotrigger.maybe_autotrigger(
            bot,
            fake_event(text=text),
            config=settings,
            incoming=incoming(text),
        )

    assert calls == []
    assert autotrigger.pending_batches("g1") == 3
    await asyncio.sleep(DEBOUNCE + 0.15)
    assert calls == ["几点起床"]


async def test_same_sender_resets_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = cfg()
    awake(settings)
    calls: list[str] = []

    async def fake_turn(inc: Incoming, **_: object) -> str:
        calls.append(inc.text)
        return ""

    monkeypatch.setattr(autotrigger, "run_turn", fake_turn)
    bot = SimpleNamespace()

    await autotrigger.maybe_autotrigger(
        bot, fake_event(text="你"), config=settings, incoming=incoming("你")
    )
    await asyncio.sleep(DEBOUNCE * 0.6)
    await autotrigger.maybe_autotrigger(
        bot, fake_event(text="明天"), config=settings, incoming=incoming("明天")
    )
    await asyncio.sleep(DEBOUNCE * 0.6)
    assert calls == []  # 距第一条已过 1.2 倍，但被第二条重置
    await asyncio.sleep(DEBOUNCE * 0.8)
    assert calls == ["明天"]


async def test_other_sender_does_not_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = cfg()
    awake(settings)
    calls: list[str] = []

    async def fake_turn(inc: Incoming, **_: object) -> str:
        calls.append(inc.text)
        return ""

    monkeypatch.setattr(autotrigger, "run_turn", fake_turn)
    bot = SimpleNamespace()

    await autotrigger.maybe_autotrigger(
        bot, fake_event(text="你"), config=settings, incoming=incoming("你")
    )
    await asyncio.sleep(DEBOUNCE * 0.6)
    await autotrigger.maybe_autotrigger(
        bot,
        fake_event(text="几点"),
        config=settings,
        incoming=incoming("几点", user_key="u2", message_id="m2"),
    )
    await asyncio.sleep(DEBOUNCE * 0.6)
    assert calls == ["几点"]  # 按第一条的时间到点，用最后一条当 current


async def test_zero_disables_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = cfg(group_presence_debounce_sec=0)
    awake(settings)
    calls: list[str] = []

    async def fake_turn(inc: Incoming, **_: object) -> str:
        calls.append(inc.text)
        return ""

    monkeypatch.setattr(autotrigger, "run_turn", fake_turn)
    await autotrigger.maybe_autotrigger(
        SimpleNamespace(),
        fake_event(text="在吗"),
        config=settings,
        incoming=incoming("在吗"),
    )
    assert calls == ["在吗"]


async def test_drops_when_window_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = cfg()
    awake(settings)
    calls: list[str] = []

    async def fake_turn(inc: Incoming, **_: object) -> str:
        calls.append(inc.text)
        return ""

    monkeypatch.setattr(autotrigger, "run_turn", fake_turn)
    await autotrigger.maybe_autotrigger(
        SimpleNamespace(),
        fake_event(text="在吗"),
        config=settings,
        incoming=incoming("在吗"),
    )
    presence.manager.close_window(
        "g1", reason="test", config=config_from(settings), now=time.monotonic()
    )
    await asyncio.sleep(DEBOUNCE + 0.15)
    assert calls == []
    assert autotrigger.pending_batches("g1") == 0


async def test_decision_uses_merged_text(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = cfg()
    awake(settings)
    calls: list[str] = []
    seen: list[str] = []

    async def fake_turn(inc: Incoming, **_: object) -> str:
        calls.append(inc.text)
        return ""

    async def fake_classify(text: str, _keyword: str) -> str:
        seen.append(text)
        return "call"

    monkeypatch.setattr(autotrigger, "run_turn", fake_turn)
    bot = SimpleNamespace()

    await autotrigger.maybe_autotrigger(
        bot,
        fake_event(text="大肥鱼"),
        config=settings,
        incoming=incoming("大肥鱼", message_id="m1"),
        classify=fake_classify,
    )
    await autotrigger.maybe_autotrigger(
        bot,
        fake_event(text="在吗"),
        config=settings,
        incoming=incoming("在吗", message_id="m2"),
        classify=fake_classify,
    )
    await asyncio.sleep(DEBOUNCE + 0.15)

    assert seen == ["大肥鱼 在吗"]  # 判定用拼接文本
    assert calls == ["在吗"]  # turn 仍用最后一条，前文在历史里
    assert presence.manager.state_of("g1", now=time.monotonic()) is PresenceState.AWAKE
