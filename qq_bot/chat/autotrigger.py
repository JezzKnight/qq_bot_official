"""群聊存在感（P1）的编排层：决策 → 发送 → 记账。

``maybe_autotrigger`` 仍是插件里的入口名。真正的状态机在
``qq_bot.chat.presence``；这里只负责：

1. 把 QQ 事件/Incoming 转成平台无关的 ``PresenceEvent``；
2. 按判定结果走 0-1 接话（主动消息）、复读（严格原样）或什么都不做；
3. 把成功/失败记回状态机。

失败一律吞掉：自动接话是锦上添花，不能因为它给用户弹错误消息。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from qq_bot.chat import presence
from qq_bot.chat.presence import (
    Action,
    Decision,
    PresenceConfig,
    PresenceEvent,
    RepeatPayload,
)
from qq_bot.chat.turn import run_turn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from qq_bot.platform.ports import Incoming

logger = logging.getLogger(__name__)

TURN_TIMEOUT_SEC = 60.0
MAX_TRACKED_GROUPS = 512
PROBE_TTL_SEC = 3600.0
AUTO_HINT = "（这没人在叫你，你只是顺嘴插一句）"

_MEDIA_SEGMENTS = frozenset(
    {"image", "face", "mface", "emoji", "ark", "keyboard", "file", "video"}
)


class ActiveReply:
    """一条主动消息的 ReplyTarget：不带 @、不发图、失败要能上报。

    ``max_parts = 1``：自动接话只说一条，避免一次刷出好几段主动消息。
    """

    uses_proactive = True
    max_parts = 1

    def __init__(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send

    async def text(self, content: str) -> None:
        await self._send(content)

    async def passive(self, content: str) -> None:
        await self._send(content)

    async def mention(self, _user_key: str, content: str) -> None:
        # 顺嘴接话不 @ 人
        await self._send(content)

    async def image(self, _data: bytes, _name: str = "") -> None:
        return None

    async def finish(self) -> None:
        return None


def reset_state() -> None:
    """清空内存状态（仅测试用；线上靠跨天/重启重置）。"""
    presence.manager.reset()


async def maybe_autotrigger(  # noqa: PLR0913 —— 入口签名由插件固定
    bot: Any,
    event: Any,
    *,
    config: Any,
    incoming: Incoming | None = None,
    dispatch: Callable[[Any, Any], Awaitable[Any]] | None = None,
    now: float | None = None,
    roll: float | None = None,
    send_text: Callable[[str], Awaitable[None]] | None = None,
    send_repeat: Callable[[RepeatPayload], Awaitable[None]] | None = None,
) -> bool:
    """群聊存在感入口：任何异常都在这里被吞掉，绝不打扰用户。"""
    try:
        return await _maybe_autotrigger(
            bot,
            event,
            config=config,
            incoming=incoming,
            dispatch=dispatch,
            now=now,
            roll=roll,
            send_text=send_text,
            send_repeat=send_repeat,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("presence crashed (swallowed)")
        return False


async def _maybe_autotrigger(  # noqa: PLR0911, PLR0913 —— 内部编排
    bot: Any,
    event: Any,
    *,
    config: Any,
    incoming: Incoming | None,
    dispatch: Callable[[Any, Any], Awaitable[Any]] | None,
    now: float | None,
    roll: float | None,
    send_text: Callable[[str], Awaitable[None]] | None,
    send_repeat: Callable[[RepeatPayload], Awaitable[None]] | None,
) -> bool:
    group_key = getattr(event, "group_openid", "") or ""
    if not group_key:
        return False
    if getattr(getattr(event, "author", None), "bot", False):
        return False
    if incoming is None:
        logger.debug("presence incoming missing, skip（应由插件传入）")
        return False

    cfg = presence.config_from(config)
    if not cfg.enabled:
        return False

    clock = _clock(now)
    presence_event = _presence_event(event, incoming, cfg)
    decision = presence.manager.observe_active(
        presence_event, config=cfg, now=clock, roll=roll
    )
    logger.info(
        "presence decide group=%s state=%s action=%s reason=%s roll=%.3f p=%.3f",
        group_key,
        decision.state.value,
        decision.action.value,
        decision.reason,
        decision.roll,
        decision.threshold,
    )
    if decision.action is Action.NONE:
        return False
    if decision.action is Action.REPEAT:
        return await _repeat(bot, event, decision, cfg, clock, send_repeat)
    return await _speak(
        bot,
        event,
        incoming,
        config=config,
        decision=decision,
        clock=clock,
        dispatch=dispatch,
        send_text=send_text,
    )


async def _speak(  # noqa: PLR0913
    bot: Any,
    event: Any,
    incoming: Incoming,
    *,
    config: Any,
    decision: Decision,
    clock: float,
    dispatch: Callable[[Any, Any], Awaitable[Any]] | None,
    send_text: Callable[[str], Awaitable[None]] | None,
) -> bool:
    """跑一轮真实 turn，把结果记为一条主动消息。"""
    group_key = decision.group_key
    text = f"{AUTO_HINT}{incoming.text}" if incoming.text else AUTO_HINT
    sender = send_text or _proactive_text(bot, event)
    try:
        if dispatch is not None:
            await asyncio.wait_for(dispatch(bot, event), timeout=TURN_TIMEOUT_SEC)
        else:
            auto_incoming = replace(incoming, text=text, reply=ActiveReply(sender))
            await asyncio.wait_for(
                run_turn(auto_incoming, config=config), timeout=TURN_TIMEOUT_SEC
            )
    except Exception as exc:  # noqa: BLE001 —— 自动接话失败只记账，不打扰用户
        code = int(getattr(exc, "code", 0) or 0)
        logger.warning(
            "presence speak failed group=%s code=%s（不重试）", group_key, code
        )
        presence.manager.note_failure(group_key, code=code, now=clock)
        return False
    presence.manager.note_spoken(group_key, now=clock)
    await _log_probe(bot, group_key, clock)
    logger.info(
        "presence speak group=%s daily=%s action=%s",
        group_key,
        presence.manager.daily_count(group_key, now=clock),
        decision.action.value,
    )
    return True


async def _repeat(  # noqa: PLR0913, PLR0917
    bot: Any,
    event: Any,
    decision: Decision,
    config: PresenceConfig,
    clock: float,
    send_repeat: Callable[[RepeatPayload], Awaitable[None]] | None,
) -> bool:
    payload = decision.payload
    if payload is None:
        return False
    sender = send_repeat or _proactive_repeat(bot, event)
    try:
        await sender(payload)
    except Exception as exc:  # noqa: BLE001 —— 复读失败不改状态机
        code = int(getattr(exc, "code", 0) or 0)
        logger.warning(
            "presence repeat failed group=%s code=%s（不改窗口）",
            decision.group_key,
            code,
        )
        return False
    presence.manager.note_repeat(decision.group_key, now=clock)
    logger.info(
        "presence repeat sent group=%s text=%r mentions=%s daily=%s/%s",
        decision.group_key,
        payload.text,
        len(payload.mentions),
        presence.manager.daily_count(decision.group_key, now=clock),
        config.daily_active,
    )
    return True


def _presence_event(
    event: Any, incoming: Incoming, config: PresenceConfig
) -> PresenceEvent:
    text = (incoming.text or "").strip()
    parts, mentions_other, repeatable = _segments(event)
    payload = (
        _repeat_payload(parts, config.repeat_max_chars)
        if repeatable and not incoming.images
        else None
    )
    return PresenceEvent(
        group_key=str(getattr(event, "group_openid", "") or ""),
        user_key=incoming.user_key,
        text=incoming.text,
        message_id=incoming.message_id,
        has_image=bool(incoming.images),
        is_command=text.startswith("/"),
        mentions_other=mentions_other,
        is_media_only=not text and not incoming.images,
        repeat_payload=payload,
    )


def _segments(event: Any) -> tuple[list[tuple[str, str]], bool, bool]:
    """拆出 (可复读的文本/@段, 是否 @ 了别人, 是否可参与复读)。"""
    parts: list[tuple[str, str]] = []
    mentions_other = False
    repeatable = True
    try:
        message = event.get_message()
    except Exception:  # noqa: BLE001 —— 没有消息体时按不可复读处理
        return parts, mentions_other, False
    for seg in message:
        if seg.type == "text":
            value = str(seg.data.get("text") or "")
            if value:
                parts.append(("text", value))
        elif seg.type == "mention_user":
            if seg.data.get("is_bot"):
                repeatable = False
            else:
                uid = str(seg.data.get("user_id") or "")
                mentions_other = True
                if uid:
                    parts.append(("mention", uid))
                else:
                    repeatable = False
        elif seg.type == "mention_everyone" or seg.type in _MEDIA_SEGMENTS:
            repeatable = False
    return parts, mentions_other, repeatable


def _repeat_payload(
    parts: list[tuple[str, str]], max_chars: int
) -> RepeatPayload | None:
    text = "".join(value for kind, value in parts if kind == "text").strip()
    if not text or text.startswith("/") or len(text) > max_chars:
        return None
    candidate = PresenceEvent(group_key="", user_key="", text=text)
    if not presence.is_acceptable(candidate):
        return None
    return RepeatPayload(tuple(parts))


def _proactive_text(bot: Any, event: Any) -> Callable[[str], Awaitable[None]]:
    async def send(content: str) -> None:
        from nonebot.adapters.qq import MessageSegment

        from qq_bot.platform.qqbot.reply import normalize_typing

        await bot.send_to_group(
            group_openid=event.group_openid,
            message=MessageSegment.text(normalize_typing(content)),
        )

    return send


def _proactive_repeat(
    bot: Any, event: Any
) -> Callable[[RepeatPayload], Awaitable[None]]:
    async def send(payload: RepeatPayload) -> None:
        from nonebot.adapters.qq import Message, MessageSegment

        segments = [
            MessageSegment.text(value)
            if kind == "text"
            else MessageSegment.mention_user(value)
            for kind, value in payload.parts
        ]
        await bot.send_to_group(
            group_openid=event.group_openid, message=Message(segments)
        )

    return send


async def probe_group_state(
    bot: Any, group_key: str, *, now: float | None = None
) -> Any:
    """探测群机器人状态（是否推非 @ 消息、是否允许主动消息）。

    只读、只记日志：每群 1 小时最多一次，连失败结果也缓存，异常一律吞掉。
    """
    if not group_key:
        return None
    clock = _clock(now)
    cached = _group_states.get(group_key)
    if cached is not None and clock - cached[0] < PROBE_TTL_SEC:
        return cached[1]
    getter = getattr(bot, "get_group_bot_state", None)
    if getter is None:
        return None
    state: Any = None
    try:
        state = await getter(group_id=group_key)
    except Exception as exc:  # noqa: BLE001 —— 探测失败不影响主流程
        logger.debug("probe_group_state failed group=%s err=%r", group_key, exc)
    if len(_group_states) >= MAX_TRACKED_GROUPS and group_key not in _group_states:
        oldest = min(_group_states, key=lambda key: _group_states[key][0])
        _group_states.pop(oldest, None)
    _group_states[group_key] = (clock, state)
    return state


async def _log_probe(bot: Any, group_key: str, clock: float) -> None:
    state = await probe_group_state(bot, group_key, now=clock)
    if state is None:
        return
    logger.info(
        "presence state group=%s recv=%s allow_proactive=%s role=%s",
        group_key,
        getattr(state, "recv_msg_setting", "?"),
        getattr(state, "allow_proactive_msg", "?"),
        getattr(state, "member_role", "?"),
    )


_group_states: dict[str, tuple[float, Any]] = {}


def _clock(now: float | None) -> float:
    return time.monotonic() if now is None else now
