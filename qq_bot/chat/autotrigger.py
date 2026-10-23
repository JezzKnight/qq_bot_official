"""自动接话的编排层：状态 → 信号 → 判定 → 借窗口跑一轮 turn。

为什么只做 L1：没有 @ 就没有 msg_id，发消息只能走主动消息（烧平台额度）。
这里只在「最近有人 @ 过 bot」的 5 分钟窗口内接话，复用那条 @ 消息的被动回复
名额，零额度成本。窗口记账取最保守的口径：一个窗口只说一句。

编排层只做三件事：维护每群状态（冷却/日额度/刷屏）、把信号凑齐交给
``activation.decide``、通过时把 reply 换成 ``WindowReply`` 再跑 ``run_turn``。
失败一律吞掉：自动接话是锦上添花，不能因为它给用户弹错误消息。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING, Any

from qq_bot.chat import activation, passive_window
from qq_bot.chat.turn import run_turn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nonebot.adapters.qq.models.qq import GroupBotStateReturn

    from qq_bot.platform.ports import Incoming

logger = logging.getLogger(__name__)

DEFAULT_DAILY_CAP = 6
DEFAULT_COOLDOWN_SEC = 90.0
DEFAULT_PROBABILITY = 0.6
DEFAULT_MIN_SCORE = 4
DEFAULT_HINT = "（这没人在叫你，你只是顺嘴插一句）"
DEFAULT_NAMES = "大肥鱼,肥鱼,鱼鱼"
TURN_TIMEOUT_SEC = 60.0  # 自动接话是抢窗口的低成本动作，不值得等 120s
STREAK_WINDOW_SEC = 60.0  # 同一个人 60 秒内连发几条
STREAK_LIMIT = 3  # 连发到这个数就不接（刷屏不像在跟 bot 说话）
OVERLAP_WINDOW_SEC = 1800.0  # 只有 30 分钟内的 bot 发言才拿来比「实词重叠」
MAX_TRACKED_GROUPS = 512  # 群级状态上限，防字典无限增长
MAX_STREAK_SENDERS = 200  # 每群发送者表上限
PROBE_TTL_SEC = 3600.0  # 群状态探测每群 1 小时最多一次（普通 HTTP）

# 只用来做「实词重叠」的虚词/代词：它们到处出现，不算信息
_STOPWORDS = frozenset(
    [
        "的", "了", "是", "我", "你", "他", "她", "它", "在", "有", "和", "就",
        "都", "也", "不", "吗", "呢", "啊", "吧", "呀", "这", "那", "个", "们",
    ]
)
_OVERLAP_RE = re.compile(r"[0-9a-zA-Z]+")


@dataclass
class GroupState:
    """每群的自动接话状态（纯内存，进程重启即清零）。"""

    last_auto_at: float = 0.0
    day: date = field(default_factory=date.today)
    count: int = 0
    # sender_key → (60s 窗口内的条数, 窗口起点)
    sender_streak: dict[str, tuple[int, float]] = field(default_factory=dict)


_states: dict[str, GroupState] = {}
_last_bot: dict[str, tuple[float, str]] = {}
_group_states: dict[str, tuple[float, GroupBotStateReturn | None]] = {}


class _PassiveUnavailableError(RuntimeError):
    """被动回复发不出去：这条自动接话作废（不降级、不重试、不烧额度）。"""

    def __init__(self, inner: object) -> None:
        super().__init__(f"passive reply unavailable: {type(inner).__name__}")


class WindowReply:
    """把被动窗口 reply 包成「只说一条、绝不 @ 人」的适配器。

    ``run_turn`` 的结尾会依次调 ``text``/``mention``/``image``/``finish``，
    这里统一压成一句纯文本：``mention`` 降级成 ``passive``（@ 一个陌生人是
    打扰，顺嘴接话只说话），``image`` 直接丢弃（不发表情包）。
    """

    uses_proactive = False  # 走被动回复，不烧主动消息额度
    max_parts = 1  # 自动接话只说一条

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def _send(self, content: str) -> None:
        """只走被动回复；失败就放弃这条，**绝不**退化成主动消息。

        降级成 ``inner.text()`` 会变成主动消息、悄悄烧掉额度，正好违背 L1
        「零额度」的前提。宁可这一句没发出去（记警告、不当成功），也不花额度。
        """
        passive = getattr(self._inner, "passive", None)
        if passive is None:
            raise _PassiveUnavailableError(self._inner)
        try:
            await passive(content)
        except Exception as exc:
            logger.warning("autotrigger passive send failed: %r", exc)
            raise _PassiveUnavailableError(self._inner) from exc

    async def text(self, content: str) -> None:
        await self._send(content)

    async def mention(self, _user_key: str, content: str) -> None:
        # 顺嘴接话不 @ 人：mention 一律降级成普通被动文本
        await self._send(content)

    async def image(self, data: bytes, name: str = "") -> None:  # noqa: ARG002
        return None

    async def finish(self) -> None:
        finish = getattr(self._inner, "finish", None)
        if finish is not None:
            await finish()


def reset_state() -> None:
    """清空内存状态（仅测试用；线上靠 TTL/跨天自然重置）。"""
    _states.clear()
    _last_bot.clear()
    _group_states.clear()


def record_bot_message(group_key: str, text: str, *, now: float | None = None) -> None:
    """记下 bot 最近一次在这个群说的话，供「实词重叠」判定。"""
    if not group_key:
        return
    clock = _clock(now)
    if len(_last_bot) >= MAX_TRACKED_GROUPS and group_key not in _last_bot:
        oldest = min(_last_bot, key=lambda key: _last_bot[key][0])
        _last_bot.pop(oldest, None)
    _last_bot[group_key] = (clock, unicodedata.normalize("NFKC", text or ""))


async def maybe_autotrigger(  # noqa: PLR0913 —— 入口签名由规格固定
    bot: Any,
    event: Any,
    *,
    config: Any,
    incoming: Incoming | None = None,
    dispatch: Callable[[Any, Any], Awaitable[Any]] | None = None,
    now: float | None = None,
) -> None:
    """群聊自动接话入口：任何异常都在这里被吞掉，绝不打扰用户。

    ``incoming`` 允许调用方传入已经构造好的对象（插件里就是这么做的，避免
    重复下载媒体）；``dispatch`` 是可选覆盖点（测试注入），默认直接
    ``run_turn``，避免 import 插件层的 ``_dispatch`` 造成循环导入。
    """
    try:
        await _maybe_autotrigger(
            bot, event, config=config, incoming=incoming, dispatch=dispatch, now=now
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("autotrigger crashed (swallowed)")


async def probe_group_state(
    bot: Any, group_key: str, *, now: float | None = None
) -> GroupBotStateReturn | None:
    """探测群机器人状态（是否推非 @ 消息、是否允许主动消息）。

    只读、只记日志：每群 1 小时最多一次，连失败结果也缓存，异常一律吞掉返回
    None —— 它只影响日志里的诊断信息，绝不阻塞或改变自动接话行为。
    """
    if not group_key:
        return None
    clock = _clock(now)
    cached = _group_states.get(group_key)
    if cached is not None and clock - cached[0] < PROBE_TTL_SEC:
        return cached[1]
    getter = getattr(bot, "get_group_bot_state", None)
    if getter is None:
        logger.debug("probe_group_state skipped group=%s: bot 不支持该 API", group_key)
        return None
    state: GroupBotStateReturn | None = None
    try:
        state = await getter(group_id=group_key)
    except Exception as exc:  # noqa: BLE001 —— 探测失败不影响主流程
        logger.debug("probe_group_state failed group=%s err=%r", group_key, exc)
    if len(_group_states) >= MAX_TRACKED_GROUPS and group_key not in _group_states:
        oldest = min(_group_states, key=lambda key: _group_states[key][0])
        _group_states.pop(oldest, None)
    _group_states[group_key] = (clock, state)
    if getattr(state, "recv_msg_setting", None) == "only_mention":
        logger.warning(
            "group=%s recv_msg_setting=only_mention：该群不推非 @ 消息，只能靠 @ 触发",
            group_key,
        )
    return state


async def _maybe_autotrigger(  # noqa: PLR0911, PLR0913 —— 早退即否决，逐个 return 最清楚
    bot: Any,
    event: Any,
    *,
    config: Any,
    incoming: Incoming | None,
    dispatch: Callable[[Any, Any], Awaitable[Any]] | None,
    now: float | None,
) -> bool:
    group_key = getattr(event, "group_openid", "") or ""
    if not group_key:
        return False
    if getattr(getattr(event, "author", None), "bot", False):
        return False
    incoming = _ensure_incoming(event, incoming)
    if incoming is None:
        return False
    text = (incoming.text or "").strip()
    if not text and not _has_media(incoming):
        return False

    clock = _clock(now)
    reply = passive_window.acquire(group_key, now=clock)
    state = _state_for(group_key)
    cooldown_sec = float(
        _cfg(config, "group_autotrigger_cooldown_sec", DEFAULT_COOLDOWN_SEC)
    )
    daily_cap = int(_cfg(config, "group_autotrigger_daily_cap", DEFAULT_DAILY_CAP))
    probability = float(
        _cfg(config, "group_autotrigger_probability", DEFAULT_PROBABILITY)
    )
    min_score = int(_cfg(config, "group_autotrigger_min_score", DEFAULT_MIN_SCORE))

    last_auto = state.last_auto_at
    cooldown_ok = last_auto == 0.0 or clock - last_auto >= cooldown_sec
    daily_ok = state.count < daily_cap
    streak = _bump_streak(state, incoming.user_key or "", clock)

    overlaps, minutes = _bot_signal(group_key, text, clock)
    # 判定只看用户原话：turn_text 前面拼了「你只是顺嘴插一句」这类提示词，里面带
    # 「你」——拿它算信号会让每条消息白送一分，也会让纯图消息躲过 media_only 否决。
    turn_text = _text_for_turn(incoming, config)
    second_person = any(word in text for word in activation.SECOND_PERSON_WORDS)
    decision = activation.decide(
        text,
        is_command=text.startswith("/"),
        has_media=_has_media(incoming),
        quoted_someone=bool(getattr(event, "reply", None)),
        mentions_bot_name=any(name in text for name in _names(config)),
        is_question=activation.looks_like_question(text),
        has_second_person=second_person,
        overlaps_last_bot_msg=overlaps,
        minutes_since_bot_spoke=minutes,
        same_sender_streak=streak,
        cooldown_ok=cooldown_ok,
        daily_budget_ok=daily_ok,
        window_available=reply is not None,
        probability=probability,
        min_score=min_score,
    )
    await _log_probe(bot, group_key, clock)
    logger.info(
        "autotrigger group=%s allowed=%s score=%s reason=%s sender=%s "
        "streak=%s daily=%s/%s text=%r",
        group_key,
        decision.allowed,
        decision.score,
        decision.reason,
        incoming.user_key,
        streak,
        state.count,
        daily_cap,
        text[:40],
    )
    if not decision.allowed:
        return False

    ok = await _run_auto_turn(
        bot,
        event,
        incoming,
        reply=WindowReply(reply),
        text=turn_text,
        config=config,
        dispatch=dispatch,
    )
    if not ok:
        return False
    passive_window.mark_used(group_key, 1, now=clock)
    state.last_auto_at = clock
    state.count += 1
    logger.info(
        "autotrigger sent group=%s daily=%s/%s", group_key, state.count, daily_cap
    )
    return True


async def _run_auto_turn(  # noqa: PLR0913 —— 内部编排，参数即上下文
    bot: Any,
    event: Any,
    incoming: Incoming,
    *,
    reply: WindowReply,
    text: str,
    config: Any,
    dispatch: Callable[[Any, Any], Awaitable[Any]] | None,
) -> bool:
    """跑一轮真实 turn；返回是否成功发出（失败只记日志，不重试、不报错）。"""
    group_key = getattr(event, "group_openid", "") or ""
    final = ""
    try:
        if dispatch is not None:
            # 覆盖链路自己负责用上 reply；这里只保证异常不外泄
            result = await asyncio.wait_for(
                dispatch(bot, event), timeout=TURN_TIMEOUT_SEC
            )
            final = result if isinstance(result, str) else ""
        else:
            auto_incoming = replace(incoming, text=text, reply=reply)
            final = await asyncio.wait_for(
                run_turn(auto_incoming, config=config), timeout=TURN_TIMEOUT_SEC
            ) or ""
    except Exception:
        logger.exception(
            "autotrigger turn failed group=%s（不重试、不打扰用户）", group_key
        )
        return False
    # 记 bot 真正说出去的那句（「实词重叠」要比的是 bot 的措辞，不是用户原话）
    record_bot_message(group_key, final)
    return True


def _ensure_incoming(_event: Any, incoming: Incoming | None) -> Incoming | None:
    """复用插件已经构造好的 Incoming，避免二次收集媒体。"""
    if incoming is None:
        # 正常链路一定传 incoming（插件里构造两次是浪费）；这里是兜底：
        # 真实发送走窗口 reply，占位 reply 不会被用到。
        logger.debug("autotrigger incoming missing, skip（应由插件传入）")
    return incoming


async def _log_probe(bot: Any, group_key: str, clock: float) -> None:
    """把群状态并进日志（诊断用；失败/不支持静默跳过）。"""
    state = await probe_group_state(bot, group_key, now=clock)
    if state is None:
        return
    logger.info(
        "autotrigger state group=%s recv=%s allow_proactive=%s role=%s",
        group_key,
        getattr(state, "recv_msg_setting", "?"),
        getattr(state, "allow_proactive_msg", "?"),
        getattr(state, "member_role", "?"),
    )


def _state_for(group_key: str) -> GroupState:
    today = date.today()  # noqa: DTZ011 —— 每日额度按本地日期重置
    state = _states.get(group_key)
    if state is None:
        if len(_states) >= MAX_TRACKED_GROUPS:
            oldest = min(_states, key=lambda key: _states[key].last_auto_at)
            _states.pop(oldest, None)
        state = GroupState(day=today)
        _states[group_key] = state
    elif state.day != today:
        # 跨天清零：每日额度按本地日期重置
        state.day = today
        state.count = 0
        state.sender_streak.clear()
    return state


def _bump_streak(state: GroupState, sender_key: str, clock: float) -> int:
    """统计同一发送者在 60 秒内连发了几条（>=STREAK_LIMIT 视为刷屏）。"""
    if not sender_key:
        return 0
    count, started = state.sender_streak.get(sender_key, (0, clock))
    if clock - started > STREAK_WINDOW_SEC:
        count, started = 0, clock
    count += 1
    state.sender_streak[sender_key] = (count, started)
    if len(state.sender_streak) > MAX_STREAK_SENDERS:
        overflow = sorted(state.sender_streak.items(), key=lambda item: item[1][1])
        for key, _ in overflow[: len(state.sender_streak) - MAX_STREAK_SENDERS]:
            del state.sender_streak[key]
    return count


def _bot_signal(group_key: str, text: str, clock: float) -> tuple[bool, float | None]:
    """bot 上一条发言 → (是否实词重叠, 距现在多少分钟)。"""
    record = _last_bot.get(group_key)
    if record is None:
        # 本进程还没记过 bot 发言：用被动窗口登记时间近似「bot 上次说话」
        return False, passive_window.minutes_since_bot_spoke(group_key, now=clock)
    at, last_text = record
    minutes = max(0.0, (clock - at) / 60.0)
    if clock - at > OVERLAP_WINDOW_SEC:
        return False, minutes
    return _overlaps(text, last_text), minutes


def _overlaps(a: str, b: str) -> bool:
    """和 bot 上一句有实词重叠（中文 2 字滑窗，英文/数字按整词）。"""
    left = unicodedata.normalize("NFKC", a or "")
    right = unicodedata.normalize("NFKC", b or "")
    tokens = set(_OVERLAP_RE.findall(right))
    tokens.update(
        bigram
        for i in range(len(right) - 1)
        if (bigram := right[i : i + 2]).isalpha() and bigram not in _STOPWORDS
    )
    return any(token in left for token in tokens)


def _text_for_turn(incoming: Incoming, config: Any) -> str:
    """给接话用的文本加提示词：模型要能分清「顺嘴接」和「被 @」。"""
    hint = _cfg(config, "group_autotrigger_hint", DEFAULT_HINT)
    return f"{hint}{incoming.text}" if hint else incoming.text


def _names(config: Any) -> list[str]:
    raw = _cfg(config, "group_autotrigger_names", DEFAULT_NAMES) or ""
    return [name.strip() for name in str(raw).split(",") if name.strip()]


def _has_media(incoming: Incoming) -> bool:
    return bool(incoming.images)


def _cfg(config: Any, name: str, default: Any) -> Any:
    """从 pydantic 配置里取值；缺字段（测试替身）时回退默认值。"""
    try:
        return getattr(config, name)
    except AttributeError:
        return default


def _clock(now: float | None) -> float:
    return time.monotonic() if now is None else now
