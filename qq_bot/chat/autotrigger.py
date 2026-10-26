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

from qq_bot.chat import conversation, participation, presence, relevance
from qq_bot.chat import judge as judge_mod
from qq_bot.chat.presence import (
    Action,
    Decision,
    PresenceConfig,
    PresenceEvent,
    PresenceState,
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
    """一条主动消息的 ReplyTarget：不带 @、失败要能上报。

    自动接话最多 4 段（与被动回复一致）；模型用空行分段时才多发。
    """

    uses_proactive = True
    max_parts = 4

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        send_image: Callable[[bytes, str], Awaitable[None]] | None = None,
        send_markdown: Callable[[str], Awaitable[None]] | None = None,
        send_quote: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._send = send
        self._send_image = send_image
        self._send_markdown = send_markdown
        self._send_quote = send_quote

    async def text(self, content: str) -> None:
        await self._send(content)

    async def passive(self, content: str) -> None:
        await self._send(content)

    async def mention(self, _user_key: str, content: str) -> None:
        # 顺嘴接话不 @ 人
        await self._send(content)

    async def quote(self, message_id: str, content: str) -> None:
        """引用某条历史消息再说话；没有引用通道时退回普通主动消息。"""
        if self._send_quote is not None:
            await self._send_quote(message_id, content)
        else:
            await self._send(content)

    async def image(self, data: bytes, name: str = "") -> None:
        if self._send_image is not None:
            await self._send_image(data, name)

    async def markdown(self, content: str) -> None:
        """查询类回答整段 Markdown 发送（msg_type=2）；没有通道时退回文本。"""
        if self._send_markdown is not None:
            await self._send_markdown(content)
        else:
            await self._send(content)

    async def finish(self) -> None:
        return None


def reset_state() -> None:
    """清空内存状态（仅测试用；线上靠跨天/重启重置）。"""
    for group_key in list(_pending_batches):
        _cancel_batch(group_key)
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
    classify: Callable[[str, str], Awaitable[Any]] | None = None,
    generate: Callable[..., Awaitable[str | None]] | None = None,
    judge: Callable[..., Awaitable[Any]] | None = None,
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
            classify=classify,
            generate=generate,
            judge=judge,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("presence crashed (swallowed)")
        return False


MUTE_REQUEST_MAX_CHARS = 10


def _looks_like_mute_request(config: presence.PresenceConfig, text: str) -> bool:
    """活跃期里没 @ 的「闭嘴」也认：短句且含静默词。"""
    body = (text or "").strip()
    if not body or len(body) > MUTE_REQUEST_MAX_CHARS:
        return False
    return any(word in body for word in config.mute_words)


class _DebounceBatch:
    """一个群的待回复批次：同人新消息重置计时，他人消息只并入。"""

    __slots__ = (
        "bot",
        "config",
        "event",
        "incoming",
        "kwargs",
        "task",
        "texts",
        "token",
        "user_key",
    )

    def __init__(
        self,
        bot: Any,
        event: Any,
        incoming: Any,
        config: Any,
        kwargs: dict[str, Any],
    ) -> None:
        self.bot = bot
        self.event = event
        self.incoming = incoming
        self.config = config
        self.kwargs = kwargs
        self.texts: list[str] = []
        self.user_key = ""
        self.token: object = object()
        self.task: asyncio.Task[None] | None = None


_pending_batches: dict[str, _DebounceBatch] = {}


def _cancel_batch(group_key: str) -> None:
    batch = _pending_batches.pop(group_key, None)
    if batch is not None and batch.task is not None:
        batch.task.cancel()


def pending_batches(group_key: str) -> int:
    """挂起批次里的消息数（测试/诊断用）。"""
    batch = _pending_batches.get(group_key)
    return len(batch.texts) if batch is not None else 0


def _schedule_debounce(  # noqa: PLR0913
    *,
    bot: Any,
    event: Any,
    incoming: Any,
    config: Any,
    delay: float,
    kwargs: dict[str, Any],
) -> None:
    """登记/重置一个群的待回复批次；到点由 ``_fire_batch`` 统一判定。"""
    group_key = str(getattr(event, "group_openid", "") or "")
    text = (incoming.text or "").strip()
    batch = _pending_batches.get(group_key)
    previous_user = batch.user_key if batch is not None else ""
    if batch is None:
        batch = _DebounceBatch(bot, event, incoming, config, kwargs)
        _pending_batches[group_key] = batch
    else:
        batch.bot = bot
        batch.event = event
        batch.incoming = incoming
        batch.config = config
        batch.kwargs = kwargs
    batch.texts.append(text)
    same_sender = not previous_user or previous_user == incoming.user_key
    batch.user_key = incoming.user_key
    if not same_sender:
        logger.info(
            "presence debounce join group=%s user=%s msgs=%s（不重置）",
            group_key,
            incoming.user_key,
            len(batch.texts),
        )
        return
    if batch.task is not None:
        batch.task.cancel()
    token = object()
    batch.token = token
    batch.task = asyncio.create_task(_fire_batch(group_key, token, delay))
    logger.info(
        "presence debounce %s group=%s user=%s msgs=%s wait=%.1f",
        "reset" if len(batch.texts) > 1 else "start",
        group_key,
        incoming.user_key,
        len(batch.texts),
        delay,
    )


async def _fire_batch(group_key: str, token: object, delay: float) -> None:
    """计时到点：窗口还在才走一次完整判定。"""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    batch = _pending_batches.get(group_key)
    if batch is None or batch.token is not token:
        return
    _pending_batches.pop(group_key, None)
    state = presence.manager.state_of(group_key, now=_clock(None))
    if state is not PresenceState.AWAKE:
        logger.info("presence debounce drop group=%s state=%s", group_key, state.value)
        return
    merged = " ".join(text for text in batch.texts if text).strip()
    logger.info(
        "presence debounce fire group=%s msgs=%s wait=%.1f",
        group_key,
        len(batch.texts),
        delay,
    )
    try:
        await _maybe_autotrigger(
            batch.bot,
            batch.event,
            config=batch.config,
            incoming=batch.incoming,
            now=None,
            decision_text=merged or None,
            skip_debounce=True,
            **batch.kwargs,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("presence debounce crashed (swallowed)")


async def _maybe_autotrigger(  # noqa: C901, PLR0911, PLR0912, PLR0913
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
    classify: Callable[[str, str], Awaitable[Any]] | None,
    generate: Callable[..., Awaitable[str | None]] | None,
    judge: Callable[..., Awaitable[Any]] | None,
    decision_text: str | None = None,
    skip_debounce: bool = False,
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
    if decision_text:
        merged = decision_text.strip()
        if merged:
            presence_event = replace(presence_event, text=merged)
    keyword_awake = False

    if (
        not skip_debounce
        and cfg.debounce_sec > 0
        and presence.manager.state_of(group_key, now=clock) is PresenceState.AWAKE
        and not presence_event.mentions_other
    ):
        _schedule_debounce(
            bot=bot,
            event=event,
            incoming=incoming,
            config=config,
            delay=cfg.debounce_sec,
            kwargs={
                "dispatch": dispatch,
                "send_text": send_text,
                "send_repeat": send_repeat,
                "classify": classify,
                "generate": generate,
                "judge": judge,
                "roll": roll,
            },
        )
        return False

    keyword = relevance.find_keyword(presence_event.text, cfg.keywords)
    if keyword and not presence_event.is_command and not presence_event.mentions_other:
        kind = await _classify(presence_event.text, keyword, classify)
        decision = presence.manager.observe_keyword(
            group_key=group_key,
            user_key=incoming.user_key,
            kind=kind,
            message_id=incoming.message_id,
            config=cfg,
            now=clock,
        )
        logger.info(
            "presence decide group=%s state=%s action=%s reason=%s kind=%s",
            group_key,
            decision.state.value,
            decision.action.value,
            decision.reason,
            kind,
        )
        if decision.action is Action.KEYWORD_CALL:
            return await _speak(
                bot,
                event,
                incoming,
                config=config,
                decision=decision,
                clock=clock,
                dispatch=dispatch,
                send_text=send_text,
                hint="",
            )
        if decision.action is Action.KEYWORD_ABOUT:
            return await _about(
                bot,
                event,
                incoming,
                keyword=keyword,
                config=cfg,
                decision=decision,
                clock=clock,
                generate=generate,
                send_text=send_text,
            )
        if decision.reason != "keyword_awake":
            return False
        keyword_awake = True

    grace = (
        not keyword_awake
        and presence.manager.in_active_grace(group_key, config=cfg, now=clock)
    )
    if grace:
        logger.info("presence grace group=%s reason=active_grace", group_key)
        # 活跃期只放宽「接不接」：@ 别人说明话题已交出去，照样退场。
        addressee = conversation.manager.addressee(group_key, bot_names=cfg.keywords)
        if addressee is not None and addressee.target not in (
            conversation.BOT_TARGET,
            conversation.GROUP_TARGET,
        ):
            presence.manager.close_window(
                group_key, reason="mention_other", config=cfg, now=clock
            )
            logger.info("presence grace exit group=%s reason=mention_other", group_key)
            return False
        # 明确让人闭嘴就闭嘴，不因为活跃期硬回。
        if not presence_event.mentions_other and _looks_like_mute_request(
            cfg, presence_event.text
        ):
            presence.manager.observe_passive(
                group_key=group_key,
                user_key=incoming.user_key,
                text=incoming.text,
                message_id=incoming.message_id,
                config=cfg,
                now=clock,
            )
            logger.info("presence grace mute group=%s", group_key)
            return False

    policy = participation.PolicyDecision(
        participation.ACTION_PRESENCE,
        "active_grace" if grace else ("keyword_awake" if keyword_awake else "off"),
    )
    if cfg.policy_enabled and not keyword_awake and not grace:
        addressee = conversation.manager.addressee(group_key, bot_names=cfg.keywords)
        relevant = conversation.manager.topic_relevant(group_key)
        policy = participation.decide(
            addressee, relevant=relevant, force_score=cfg.policy_force_score
        )
        logger.info(
            "presence policy group=%s target=%s score=%.2f action=%s reason=%s",
            group_key,
            addressee.target if addressee is not None else "-",
            addressee.score if addressee is not None else 0.0,
            policy.action,
            policy.reason,
        )

    policy_pass = not grace and policy.action == participation.ACTION_PASS
    force = (
        keyword_awake or grace or policy.action == participation.ACTION_REPLY
    )
    if (
        not force
        and not policy_pass
        and cfg.judge_enabled
        and presence.manager.state_of(group_key, now=clock) is PresenceState.AWAKE
    ):
        if not await _judge_and_apply(
            group_key,
            incoming,
            presence_event,
            cfg,
            clock,
            judge,
        ):
            return False
        force = True

    decision = presence.manager.observe_active(
        presence_event,
        config=cfg,
        now=clock,
        roll=roll,
        force=force,
        policy_pass=policy_pass,
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
    hint: str = AUTO_HINT,
) -> bool:
    """跑一轮真实 turn，把结果记为一条主动消息。"""
    group_key = decision.group_key
    sender = send_text or _proactive_text(bot, event)
    body = ""
    try:
        if dispatch is not None:
            await asyncio.wait_for(dispatch(bot, event), timeout=TURN_TIMEOUT_SEC)
        else:
            auto_incoming = replace(
                incoming,
                hint=hint,
                reply=ActiveReply(
                    sender,
                    _proactive_image(bot, event),
                    _proactive_markdown(bot, event),
                    _proactive_quote(bot, event),
                ),
            )
            body = await asyncio.wait_for(
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
    conversation.manager.note_bot(group_key, str(body or ""), at=clock)
    await _log_probe(bot, group_key, clock)
    logger.info(
        "presence speak group=%s daily=%s action=%s",
        group_key,
        presence.manager.daily_count(group_key, now=clock),
        decision.action.value,
    )
    return True


async def _about(  # noqa: PLR0913 —— 过渡句需要这些上下文
    bot: Any,
    event: Any,
    incoming: Incoming,
    *,
    keyword: str,
    config: PresenceConfig,
    decision: Decision,
    clock: float,
    generate: Callable[..., Awaitable[str | None]] | None,
    send_text: Callable[[str], Awaitable[None]] | None,
) -> bool:
    """``ABOUT``：生成过渡句后主动发出，不跑工具链。"""
    group_key = decision.group_key
    text = await _transition(
        keyword=keyword, incoming=incoming, config=config, generate=generate
    )
    sender = send_text or _proactive_text(bot, event)
    try:
        await sender(text)
    except Exception as exc:  # noqa: BLE001 —— 过渡失败只记账
        code = int(getattr(exc, "code", 0) or 0)
        logger.warning(
            "presence transition failed group=%s code=%s（不重试）", group_key, code
        )
        presence.manager.note_failure(group_key, code=code, now=clock)
        return False
    presence.manager.note_spoken(group_key, now=clock)
    conversation.manager.note_bot(group_key, text, at=clock)
    logger.info(
        "presence transition group=%s keyword=%s text=%r daily=%s",
        group_key,
        keyword,
        text,
        presence.manager.daily_count(group_key, now=clock),
    )
    return True


async def _transition(
    *,
    keyword: str,
    incoming: Incoming,
    config: PresenceConfig,
    generate: Callable[..., Awaitable[str | None]] | None,
) -> str:
    """生成过渡句；任何失败都回退到兜底句。"""
    nick = incoming.display_name or "有人"
    if generate is not None:
        value = await generate(keyword=keyword, text=incoming.text, nick=nick)
    else:
        window, summary = await _keyword_context(incoming, config)
        value = await relevance.generate_transition(
            keyword=keyword,
            text=incoming.text,
            nick=nick,
            group_window=window,
            summary=summary,
        )
    return value or config.keyword_fallback


async def _classify(
    text: str,
    keyword: str,
    classify: Callable[[str, str], Awaitable[Any]] | None,
) -> str:
    if classify is not None:
        value = await classify(text, keyword)
    else:
        value = await relevance.classify(text, keyword)
    return str(getattr(value, "value", value))


async def _keyword_context(
    incoming: Incoming, config: PresenceConfig
) -> tuple[list[Any], str | None]:
    """读群摘要与对话记录窗口，失败就当没有上下文。"""
    try:
        from qq_bot.services.memory_writing import get_memory

        memory = await get_memory()
        if memory is None:
            return [], None
        window = await memory.recent_group_messages(
            incoming.group_key, config.keyword_context_messages
        )
        summary = await memory.latest_summary(incoming.group_key)
        return [m for m in window if m.role != "tool"], summary
    except Exception:
        logger.warning("keyword context unavailable, fallback", exc_info=True)
        return [], None


async def _judge_and_apply(  # noqa: PLR0913, PLR0917 —— 需要完整上下文
    group_key: str,
    incoming: Incoming,
    presence_event: PresenceEvent,
    config: PresenceConfig,
    clock: float,
    judge_fn: Callable[..., Awaitable[Any]] | None,
) -> bool:
    """AWAKE 窗口内的三态判定；返回 True 表示可以接这条消息。"""
    if presence.manager.state_of(group_key, now=clock) is not PresenceState.AWAKE:
        return False
    if presence_event.mentions_other or not presence.is_acceptable(presence_event):
        return False
    action = await _judge_action(incoming, presence_event, config, clock, judge_fn)
    if action is judge_mod.JudgeAction.SLEEP:
        presence.manager.close_window(
            group_key, reason="model_left", config=config, now=clock
        )
        logger.info("presence judge group=%s action=sleep", group_key)
        return False
    if action is judge_mod.JudgeAction.PASS:
        closed = presence.manager.note_pass(group_key, config=config, now=clock)
        logger.info(
            "presence judge group=%s action=pass closed=%s", group_key, closed
        )
        return False
    logger.info("presence judge group=%s action=reply", group_key)
    return True


async def _judge_action(
    incoming: Incoming,
    presence_event: PresenceEvent,
    config: PresenceConfig,
    clock: float,  # noqa: ARG001 —— 保留签名，便于以后按时间加权
    judge_fn: Callable[..., Awaitable[Any]] | None,
) -> Any:
    """调判定模型；上下文缺失或失败时一律 PASS。"""
    recent: list[dict[str, str]] = []
    bot_last = ""
    try:
        from qq_bot.services.memory_writing import get_memory

        memory = await get_memory()
        if memory is not None:
            window = await memory.recent_group_messages(
                incoming.group_key, config.judge_context_messages
            )
            window = [m for m in window if m.role != "tool"]
            recent = [
                {
                    "role": message.role,
                    "name": message.sender_name or "",
                    "content": message.content or "",
                }
                for message in window
            ]
            bot_last = next(
                (
                    message.content or ""
                    for message in reversed(window)
                    if message.role == "assistant"
                ),
                "",
            )
    except Exception:
        logger.debug("judge context unavailable", exc_info=True)

    fn = judge_fn or judge_mod.judge_in_window
    try:
        return await fn(
            text=presence_event.text,
            speaker=incoming.display_name or "",
            recent=recent,
            bot_last=bot_last,
        )
    except Exception:
        logger.warning("presence judge call failed, treat as pass", exc_info=True)
        return judge_mod.JudgeAction.PASS


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
    conversation.manager.note_bot(decision.group_key, payload.text, at=clock)
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


def _proactive_image(
    bot: Any, event: Any
) -> Callable[[bytes, str], Awaitable[None]]:
    """自动接话时的表情包：主动消息优先，发不出去再退回被动（不烧额度）。"""

    async def send(data: bytes, name: str = "") -> None:
        from nonebot.adapters.qq import MessageSegment
        from nonebot.adapters.qq.exception import ActionFailed

        from qq_bot.platform.qqbot.reply import CONTENT_VIOLATION_CODE

        seg = MessageSegment.file_image(data, name or "image.png")
        try:
            await bot.send_to_group(
                group_openid=event.group_openid,
                message=seg,
            )
        except ActionFailed as exc:
            if exc.code == CONTENT_VIOLATION_CODE:  # 内容违规，换通道也一样被拒
                raise
            logger.warning("presence image 主动发送失败，回退被动", exc_info=True)
            await bot.send(event, seg)
        except Exception:
            logger.warning("presence image 主动发送失败，回退被动", exc_info=True)
            await bot.send(event, seg)

    return send


def _proactive_markdown(bot: Any, event: Any) -> Callable[[str], Awaitable[None]]:
    async def send(content: str) -> None:
        from nonebot.adapters.qq import MessageSegment

        await bot.send_to_group(
            group_openid=event.group_openid,
            message=MessageSegment.markdown(content),
        )

    return send


def _proactive_quote(
    bot: Any, event: Any
) -> Callable[[str, str], Awaitable[None]]:
    """引用某条历史消息的主动发送；失败降级为普通主动消息。"""

    async def send(message_id: str, content: str) -> None:
        from nonebot.adapters.qq import Message, MessageSegment

        from qq_bot.platform.qqbot.reply import normalize_typing

        text = MessageSegment.text(normalize_typing(content))
        try:
            await bot.send_to_group(
                group_openid=event.group_openid,
                message=Message([MessageSegment.reference(message_id), text]),
            )
        except Exception:
            logger.warning("presence quote 发送失败，退化为普通消息", exc_info=True)
            await bot.send_to_group(group_openid=event.group_openid, message=text)

    return send


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
