"""群存在感状态机（P1）。

状态只有三个：``SLEEPING`` / ``AWAKE`` / ``MUTED``。被动回复是通道能力，
不是状态；复读机也不改变状态。模块只做决策与记账，发送与落库由
``qq_bot.chat.autotrigger`` 负责。

时间一律由调用方注入（``now``），随机数也由调用方注入（``roll``），
所以测试里不需要 sleep，也不依赖真实随机。
"""

from __future__ import annotations

import logging
import random
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

MAX_GROUPS = 512
MAX_REPEAT_KEYS = 2048
MAX_RECENT_IDS = 64
DEFAULT_MUTE_WORDS = ("闭嘴", "别说了", "安静")
DEFAULT_MUTE_ACK = "好"

# 平台错误码（qq_bot/platform/qqbot/reply.py 里也有定义，这里只用于失败分类）
PROACTIVE_NO_PERMISSION = 40034105
CONTENT_VIOLATION = 40034006
RATE_LIMITED = 429
FAILURE_LIMIT = 2  # 连续失败几次就当天关闭主动

_PURE_LINK = re.compile(r"https?://\S+", re.IGNORECASE)
_TRAILING = "。.!！?？~～…，,、;；:：\"'“”‘’）)】]》」 \t\n"  # noqa: RUF001


class PresenceState(str, Enum):
    """群里的存在感状态。"""

    SLEEPING = "sleeping"
    AWAKE = "awake"
    MUTED = "muted"


class Action(str, Enum):
    """一次判定的结果。"""

    NONE = "none"
    WAKE = "wake"
    IN_WINDOW = "in_window"
    REPEAT = "repeat"
    MUTE_ACK = "mute_ack"
    UNMUTE = "unmute"
    KEYWORD_CALL = "keyword_call"
    KEYWORD_ABOUT = "keyword_about"
    HOOK = "hook"


@dataclass(frozen=True)
class RepeatPayload:
    """复读要原样发出去的内容：按原消息段顺序保存（text / mention）。"""

    parts: tuple[tuple[str, str], ...]

    @property
    def mentions(self) -> tuple[str, ...]:
        return tuple(value for kind, value in self.parts if kind == "mention")

    @property
    def text(self) -> str:
        return "".join(value for kind, value in self.parts if kind == "text")


@dataclass(frozen=True)
class PresenceEvent:
    """一条群消息里与存在感判定有关的信息。"""

    group_key: str
    user_key: str
    text: str
    message_id: str = ""
    has_image: bool = False
    is_command: bool = False
    mentions_other: bool = False
    is_media_only: bool = False
    repeat_payload: RepeatPayload | None = None


@dataclass(frozen=True)
class PresenceConfig:
    """参数默认值见 docs/spec/group-presence.md §7。"""

    enabled: bool = True
    rate: float = 0.2
    window_sec: float = 600.0
    rate_in_window: float = 2.0
    reply_cooldown_sec: float = 45.0
    wake_cooldown_sec: float = 300.0
    daily_active: int = 120
    mute_sec: float = 1800.0
    mute_words: tuple[str, ...] = DEFAULT_MUTE_WORDS
    mute_ack: str = DEFAULT_MUTE_ACK
    ignore_openids: frozenset[str] = frozenset()
    image_rate_mult: float = 0.5
    repeat_enabled: bool = True
    repeat_min_users: int = 3
    repeat_window_sec: float = 90.0
    repeat_max_chars: int = 20
    keywords: tuple[str, ...] = ("大肥鱼",)
    keyword_cooldown_sec: float = 120.0
    keyword_user_cooldown_sec: float = 300.0
    keyword_window_sec: float = 300.0
    hook_sec: float = 120.0
    hook_rate: float = 1.0
    keyword_homonym_mode: str = "skip"
    keyword_context_messages: int = 15
    keyword_fallback: str = "什么意思"


@dataclass
class Decision:
    """判定结果；``action`` 决定上层怎么发。"""

    action: Action
    reason: str
    group_key: str
    state: PresenceState
    roll: float = 0.0
    threshold: float = 0.0
    payload: RepeatPayload | None = None
    ack: str = ""
    mute_until: float = 0.0


@dataclass
class GroupPresence:
    """每群内存状态；进程重启清零（规格已确认可接受）。"""

    state: PresenceState = PresenceState.SLEEPING
    expires_at: float = 0.0
    last_spoke_at: float = 0.0
    wake_cooldown_until: float = 0.0
    mute_until: float = 0.0
    mute_trigger_id: str = ""
    day: str = ""
    daily_count: int = 0
    active_ok: bool = True
    failures: int = 0
    recent_ids: list[str] = field(default_factory=list)
    keyword_until: float = 0.0
    user_keyword_at: dict[str, float] = field(default_factory=dict)
    hook_until: float = 0.0
    hook_used: bool = False

    def reset_day(self, today: str) -> None:
        if self.day != today:
            self.day = today
            self.daily_count = 0
            self.active_ok = True
            self.failures = 0


@dataclass
class RepeatEntry:
    """一句话在统计窗口内的「不同用户」计数。"""

    users: dict[str, float] = field(default_factory=dict)
    first_at: float = 0.0
    last_at: float = 0.0
    spoken_at: float = float("-inf")


def is_acceptable(event: PresenceEvent) -> bool:
    """可接性过滤：只看形式，不做语义判断（规格 §6.6）。"""
    if event.is_command:
        return False
    if event.mentions_other:
        return False
    text = (event.text or "").strip()
    if event.is_media_only and not event.has_image:
        return False
    if not text and not event.has_image:
        return False
    return not (text and _PURE_LINK.fullmatch(text))


def normalize_text(text: str) -> str:
    """复读比较用的归一化：全角转半角、去首尾空白与尾部标点。"""
    value = unicodedata.normalize("NFKC", text or "").strip()
    return value.rstrip(_TRAILING)


class PresenceManager:
    """状态机本体；全局单例见模块底部的 ``manager``。"""

    def __init__(self) -> None:
        self._groups: dict[str, GroupPresence] = {}
        self._repeats: dict[str, dict[tuple[tuple[str, ...], str], RepeatEntry]] = {}

    def reset(self) -> None:
        """清空内存状态（仅测试用）。"""
        self._groups.clear()
        self._repeats.clear()

    # ── 被动路径（@bot）──────────────────────────────────────────────

    def observe_passive(  # noqa: PLR0913
        self,
        *,
        group_key: str,
        user_key: str,
        text: str,
        message_id: str,
        config: PresenceConfig,
        now: float,
    ) -> Decision:
        """@ 路径的前置判定：静默词、静默解除。不参与 0-1。"""
        if not config.enabled:
            return Decision(Action.NONE, "disabled", group_key, PresenceState.SLEEPING)
        group = self._group(group_key, now)
        if any(word in (text or "") for word in config.mute_words):
            group.state = PresenceState.MUTED
            group.mute_until = now + config.mute_sec
            group.mute_trigger_id = message_id
            group.hook_until = 0.0
            logger.info(
                "presence mute group=%s by=%s trigger=%s until=%.0f",
                group_key,
                user_key,
                message_id,
                group.mute_until,
            )
            return Decision(
                Action.MUTE_ACK,
                "mute",
                group_key,
                group.state,
                ack=config.mute_ack,
                mute_until=group.mute_until,
            )
        if group.state is PresenceState.MUTED:
            if message_id and message_id == group.mute_trigger_id:
                # 触发静默那条消息的回执不能解除静默（规格 §6.4）
                return Decision(Action.NONE, "mute_ack_self", group_key, group.state)
            group.state = PresenceState.SLEEPING
            group.mute_until = 0.0
            group.mute_trigger_id = ""
            logger.info("presence unmute group=%s reason=passive", group_key)
            return Decision(Action.UNMUTE, "passive", group_key, group.state)
        return Decision(Action.NONE, "passive", group_key, group.state)

    # ── 关键词路径（名字出现）────────────────────────────────────────

    def observe_keyword(  # noqa: C901, PLR0911, PLR0913
        self,
        *,
        group_key: str,
        user_key: str,
        kind: str,
        message_id: str,
        config: PresenceConfig,
        now: float,
    ) -> Decision:
        """名字命中后的判定；``kind`` 是 ``relevance.KeywordKind`` 的值。"""
        group = self._group(group_key, now)
        self._expire(group_key, group, now)
        if not config.enabled:
            return self._skip_group(group_key, group, "disabled")
        if group.state is PresenceState.MUTED:
            return self._skip_group(group_key, group, "muted")
        if user_key in config.ignore_openids:
            return self._skip_group(group_key, group, "ignore")
        if not group.active_ok:
            return self._skip_group(group_key, group, "proactive_disabled")
        if message_id and message_id in group.recent_ids:
            return self._skip_group(group_key, group, "duplicate")
        self._remember(group, message_id)
        if kind == "homonym":
            return self._skip_group(group_key, group, "homonym")
        if group.state is PresenceState.AWAKE:
            # 窗口里再提名字：不重复开窗、不重复过渡，交回 0-1 正常判定
            return self._skip_group(group_key, group, "keyword_awake")
        if group.daily_count >= config.daily_active:
            return self._skip_group(group_key, group, "daily_cap")
        if now < group.keyword_until:
            return self._skip_group(group_key, group, "keyword_cooldown")
        user_last = group.user_keyword_at.get(user_key, float("-inf"))
        if now < user_last + config.keyword_user_cooldown_sec:
            return self._skip_group(group_key, group, "keyword_user_cooldown")

        group.keyword_until = now + config.keyword_cooldown_sec
        group.user_keyword_at[user_key] = now
        is_call = kind == "call"
        if not is_call:
            # 只有 ABOUT 才开场；CALL 只回一次，不把 bot 留在场上刷屏。
            group.state = PresenceState.AWAKE
            group.expires_at = now + config.keyword_window_sec
            group.hook_until = now + config.hook_sec
            group.hook_used = False
        logger.info(
            "presence keyword group=%s by=%s kind=%s expires=%.0f",
            group_key,
            user_key,
            kind,
            group.expires_at,
        )
        return Decision(
            Action.KEYWORD_CALL if is_call else Action.KEYWORD_ABOUT,
            kind,
            group_key,
            group.state,
            threshold=config.hook_rate,
        )

    # ── 主动路径（0-1 / 复读）────────────────────────────────────────

    def observe_active(  # noqa: C901, PLR0911
        self,
        event: PresenceEvent,
        *,
        config: PresenceConfig,
        now: float,
        roll: float | None = None,
    ) -> Decision:
        """一条非 @ 群消息的完整判定。"""
        group = self._group(event.group_key, now)
        if self._expire(event.group_key, group, now):
            return self._skip(event, group, "window_timeout")

        if not config.enabled:
            return self._skip(event, group, "disabled")
        if group.state is PresenceState.MUTED:
            return self._skip(event, group, "muted")
        if event.user_key in config.ignore_openids:
            return self._skip(event, group, "ignore")
        if not group.active_ok:
            return self._skip(event, group, "proactive_disabled")
        if event.message_id and event.message_id in group.recent_ids:
            return self._skip(event, group, "duplicate")
        self._remember(group, event.message_id)

        if (
            group.hook_until > now
            and not group.hook_used
            and is_acceptable(event)
            and (config.hook_rate >= 1.0 or _roll(roll) < config.hook_rate)
        ):
            group.hook_used = True
            group.hook_until = 0.0
            logger.info(
                "presence hook group=%s roll=%.3f", event.group_key, _roll(roll)
            )
            return Decision(Action.HOOK, "hook", event.group_key, group.state)

        if (
            config.repeat_enabled
            and event.repeat_payload is not None
            and self._repeat_hit(event, config, now)
        ):
            logger.info(
                "presence repeat group=%s text=%r mentions=%s",
                event.group_key,
                event.repeat_payload.text,
                len(event.repeat_payload.mentions),
            )
            return Decision(
                Action.REPEAT,
                "repeat",
                event.group_key,
                group.state,
                payload=event.repeat_payload,
            )

        if not is_acceptable(event):
            return self._skip(event, group, "not_acceptable")

        if group.state is PresenceState.AWAKE:
            return self._decide_awake(event, group, config, now, roll)

        return self._decide_sleeping(event, group, config, now, roll)

    def note_spoken(self, group_key: str, *, now: float) -> None:
        """主动消息发出后记账。"""
        group = self._group(group_key, now)
        group.last_spoke_at = now
        group.daily_count += 1
        group.failures = 0

    def note_passive(self, group_key: str, *, now: float) -> None:
        """被动回复发出后只更新「上次说话时间」，不占日额度。"""
        group = self._group(group_key, now)
        group.last_spoke_at = now
        group.failures = 0

    def note_repeat(self, group_key: str, *, now: float) -> None:
        """复读计入日上限，但**不**写 ``last_spoke_at``（不占发言冷却）。"""
        group = self._group(group_key, now)
        group.daily_count += 1

    def note_failure(self, group_key: str, *, code: int = 0, now: float) -> None:
        """发送失败：关窗并按错误码熔断（规格 §10）。"""
        group = self._group(group_key, now)
        group.state = PresenceState.SLEEPING
        group.expires_at = 0.0
        if code == PROACTIVE_NO_PERMISSION:
            group.active_ok = False
            group.wake_cooldown_until = now + 300.0
        elif code == CONTENT_VIOLATION:
            group.wake_cooldown_until = now + 300.0
        elif code == RATE_LIMITED:
            group.wake_cooldown_until = now + 60.0
        else:
            group.failures += 1
            group.wake_cooldown_until = now + 30.0
            if group.failures >= FAILURE_LIMIT:
                group.active_ok = False
        logger.warning(
            "presence error group=%s code=%s failures=%s active_ok=%s",
            group_key,
            code,
            group.failures,
            group.active_ok,
        )

    def state_of(self, group_key: str, *, now: float) -> PresenceState:
        """读取当前状态（测试/日志用，会先处理到期）。"""
        group = self._group(group_key, now)
        self._expire(group_key, group, now)
        return group.state

    def daily_count(self, group_key: str, *, now: float) -> int:
        return self._group(group_key, now).daily_count

    # ── 内部 ────────────────────────────────────────────────────────

    def _decide_awake(
        self,
        event: PresenceEvent,
        group: GroupPresence,
        config: PresenceConfig,
        now: float,
        roll: float | None,
    ) -> Decision:
        if now < group.last_spoke_at + config.reply_cooldown_sec:
            return self._skip(event, group, "cooldown")
        if group.daily_count >= config.daily_active:
            self._close(event.group_key, group, "daily_cap", config, now)
            return self._skip(event, group, "daily_cap")
        threshold = _clamp(config.rate * config.rate_in_window)
        if event.has_image:
            threshold *= config.image_rate_mult
        draw = _roll(roll)
        if draw < threshold:
            return Decision(
                Action.IN_WINDOW,
                "rate",
                event.group_key,
                group.state,
                roll=draw,
                threshold=threshold,
            )
        return self._skip(event, group, "rate", roll=draw, threshold=threshold)

    def _decide_sleeping(
        self,
        event: PresenceEvent,
        group: GroupPresence,
        config: PresenceConfig,
        now: float,
        roll: float | None,
    ) -> Decision:
        if now < group.wake_cooldown_until:
            return self._skip(event, group, "cooldown")
        if group.daily_count >= config.daily_active:
            return self._skip(event, group, "daily_cap")
        threshold = _clamp(config.rate)
        if event.has_image:
            threshold *= config.image_rate_mult
        draw = _roll(roll)
        if draw >= threshold:
            return self._skip(event, group, "rate", roll=draw, threshold=threshold)
        group.state = PresenceState.AWAKE
        group.expires_at = now + config.window_sec
        logger.info(
            "presence wake group=%s expires=%.0f threshold=%.3f",
            event.group_key,
            group.expires_at,
            threshold,
        )
        return Decision(
            Action.WAKE,
            "wake",
            event.group_key,
            group.state,
            roll=draw,
            threshold=threshold,
        )

    def _repeat_hit(
        self, event: PresenceEvent, config: PresenceConfig, now: float
    ) -> bool:
        payload = event.repeat_payload
        if payload is None:
            return False
        key = (payload.mentions, normalize_text(payload.text))
        entries = self._repeats.setdefault(event.group_key, {})
        entry = entries.get(key)
        if entry is None or now - entry.last_at > config.repeat_window_sec:
            entry = RepeatEntry(first_at=now, last_at=now)
            entries[key] = entry
            self._trim_repeats(entries)
        entry.last_at = now
        entry.users[event.user_key] = now
        if len(entry.users) < config.repeat_min_users:
            return False
        # 同一波只复读一次：复读后 REPEAT_WINDOW_SEC 内不再重复（去重，不是限频）
        if now - entry.spoken_at <= config.repeat_window_sec:
            return False
        entry.spoken_at = now
        return True

    def _trim_repeats(
        self, entries: dict[tuple[tuple[str, ...], str], RepeatEntry]
    ) -> None:
        if len(entries) <= MAX_REPEAT_KEYS:
            return
        overflow = sorted(entries.items(), key=lambda item: item[1].last_at)
        for key, _ in overflow[: len(entries) - MAX_REPEAT_KEYS]:
            entries.pop(key, None)

    def _expire(self, group_key: str, group: GroupPresence, now: float) -> bool:
        """处理静默/窗口到期；窗口到期返回 True（本条消息不再抽奖）。"""
        if group.state is PresenceState.MUTED and now >= group.mute_until:
            group.state = PresenceState.SLEEPING
            group.mute_until = 0.0
            group.mute_trigger_id = ""
            group.hook_until = 0.0
        if group.state is PresenceState.AWAKE and now >= group.expires_at:
            self._close(group_key, group, "timeout", None, now)
            return True
        return False

    def _close(
        self,
        group_key: str,
        group: GroupPresence,
        reason: str,
        config: PresenceConfig | None,
        now: float,
    ) -> None:
        if group.state is not PresenceState.AWAKE:
            return
        group.state = PresenceState.SLEEPING
        group.expires_at = 0.0
        group.hook_until = 0.0
        group.wake_cooldown_until = now + (
            config.wake_cooldown_sec if config is not None else 300.0
        )
        logger.info(
            "presence exit group=%s reason=%s cooldown=%.0f",
            group_key,
            reason,
            group.wake_cooldown_until,
        )

    def _remember(self, group: GroupPresence, message_id: str) -> None:
        if not message_id:
            return
        group.recent_ids.append(message_id)
        if len(group.recent_ids) > MAX_RECENT_IDS:
            del group.recent_ids[: len(group.recent_ids) - MAX_RECENT_IDS]

    def _group(self, group_key: str, now: float) -> GroupPresence:  # noqa: ARG002
        today = date.today().isoformat()  # noqa: DTZ011 —— 日额度按本地日期重置
        group = self._groups.get(group_key)
        if group is None:
            if len(self._groups) >= MAX_GROUPS:
                oldest = min(
                    self._groups, key=lambda key: self._groups[key].last_spoke_at
                )
                self._groups.pop(oldest, None)
                self._repeats.pop(oldest, None)
            group = GroupPresence(day=today)
            self._groups[group_key] = group
        group.reset_day(today)
        return group

    def _skip(
        self,
        event: PresenceEvent,
        group: GroupPresence,
        reason: str,
        *,
        roll: float = 0.0,
        threshold: float = 0.0,
    ) -> Decision:
        logger.info(
            "presence skip group=%s reason=%s state=%s roll=%.3f p=%.3f",
            event.group_key,
            reason,
            group.state.value,
            roll,
            threshold,
        )
        return Decision(
            Action.NONE,
            reason,
            event.group_key,
            group.state,
            roll=roll,
            threshold=threshold,
        )

    def _skip_group(
        self, group_key: str, group: GroupPresence, reason: str
    ) -> Decision:
        logger.info(
            "presence skip group=%s reason=%s state=%s",
            group_key,
            reason,
            group.state.value,
        )
        return Decision(Action.NONE, reason, group_key, group.state)


def _roll(value: float | None) -> float:
    return random.random() if value is None else value


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _split_words(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def config_from(settings: Any) -> PresenceConfig:
    """把 pydantic 配置（或测试替身）转成不可变配置。"""

    def get(name: str, default: Any) -> Any:
        value = getattr(settings, name, default)
        return default if value is None else value

    return PresenceConfig(
        enabled=bool(get("group_presence_enabled", True)),
        rate=float(get("group_presence_rate", 0.2)),
        window_sec=float(get("group_presence_window_sec", 600)),
        rate_in_window=float(get("group_presence_rate_in_window", 2.0)),
        reply_cooldown_sec=float(get("group_presence_reply_cooldown_sec", 45)),
        wake_cooldown_sec=float(get("group_presence_wake_cooldown_sec", 300)),
        daily_active=int(get("group_presence_daily_active", 120)),
        mute_sec=float(get("group_presence_mute_sec", 1800)),
        mute_words=_split_words(get("group_presence_mute_words", "闭嘴,别说了,安静")),
        mute_ack=str(get("group_presence_mute_ack", "好")),
        ignore_openids=frozenset(
            _split_words(get("group_presence_ignore_openids", ""))
        ),
        image_rate_mult=float(get("group_presence_image_rate_mult", 0.5)),
        repeat_enabled=bool(get("group_presence_repeat_enabled", True)),
        repeat_min_users=int(get("group_presence_repeat_min_users", 3)),
        repeat_window_sec=float(get("group_presence_repeat_window_sec", 90)),
        repeat_max_chars=int(get("group_presence_repeat_max_chars", 20)),
        keywords=_split_words(get("group_presence_keywords", "大肥鱼")),
        keyword_cooldown_sec=float(
            get("group_presence_keyword_cooldown_sec", 120)
        ),
        keyword_user_cooldown_sec=float(
            get("group_presence_keyword_user_cooldown_sec", 300)
        ),
        keyword_window_sec=float(get("group_presence_keyword_window_sec", 300)),
        hook_sec=float(get("group_presence_hook_sec", 120)),
        hook_rate=float(get("group_presence_hook_rate", 1.0)),
        keyword_homonym_mode=str(get("group_presence_keyword_homonym_mode", "skip")),
        keyword_context_messages=int(
            get("group_presence_keyword_context_messages", 15)
        ),
        keyword_fallback=str(get("group_presence_keyword_fallback", "什么意思")),
    )


manager = PresenceManager()
