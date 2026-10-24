"""群聊会话图：消息缓冲、发言账本、指向解析与 ``[群聊状态]`` 注入。

P1 只做三件事：维护每群最近若干条消息、按人聚合发言统计、解析当前消息在
跟谁说话。不改变回复判定，也不做持久化；重启后从空开始。
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from itertools import pairwise

from qq_bot.chat.relevance import KeywordKind, fast_kind, find_keyword

MAX_GROUPS = 512
DEFAULT_WINDOW_SEC = 600.0
DEFAULT_MAX_MESSAGES = 30
DEFAULT_BLOCK_TOKENS = 80
DEFAULT_MAX_SPEAKERS = 5
DEFAULT_MAX_PHRASES = 3
DEFAULT_TOPIC_WINDOW_SEC = 600.0
DEFAULT_BOT_NAMES: tuple[str, ...] = ("大肥鱼",)
UNKNOWN_NAME = "有人"
BOT_TARGET = "bot"
GROUP_TARGET = "group"

_WHITESPACE = re.compile(r"\s+")
_SECOND_PERSON = ("你", "你们")
_QUESTION_ENDINGS = ("吗", "呢", "?", "？")
_MAX_PHRASE_CHARS = 16
_MIN_PAIR_CHARS = 2
_TIMES = "\u00d7"


def normalize_text(text: str) -> str:
    """全角转半角、小写、压缩空白；账本与匹配都用这个口径。"""
    value = unicodedata.normalize("NFKC", text or "").lower()
    return _WHITESPACE.sub(" ", value).strip()


def topic_tokens(text: str) -> frozenset[str]:
    """取汉字/字母数字的字符二元组，作为话题词。"""
    chars = [
        char
        for char in normalize_text(text)
        if not char.isspace() and (char.isalnum() or "\u4e00" <= char <= "\u9fff")
    ]
    if len(chars) < _MIN_PAIR_CHARS:
        return frozenset(chars)
    return frozenset("".join(pair) for pair in pairwise(chars))


def parse_bot_names(raw: str) -> tuple[str, ...]:
    """把 ``GROUP_PRESENCE_KEYWORDS`` 这种逗号分隔串解析成名字元组。"""
    names = tuple(item.strip() for item in (raw or "").split(",") if item.strip())
    return names or DEFAULT_BOT_NAMES


@dataclass(slots=True)
class MessageNode:
    """一条群消息；``text`` 不含引用前缀，``tokens`` 供话题重叠用。"""

    mid: str
    sender_id: str
    sender_name: str
    text: str
    at: float
    mentions: tuple[str, ...] = ()
    mention_bot: bool = False
    reply_to: str | None = None
    tokens: frozenset[str] = frozenset()


@dataclass(slots=True)
class SpeakerStat:
    """窗口内某个人的发言统计。"""

    user_id: str
    name: str
    count: int = 0
    texts: Counter[str] = field(default_factory=Counter)
    last_at: float = 0.0


@dataclass(frozen=True, slots=True)
class Addressee:
    """当前消息指向：``bot`` / 某个 openid / ``group``。"""

    target: str
    score: float
    evidence: tuple[str, ...] = ()


class SpeakerLedger:
    """按 openid 聚合的发言账本，只保留窗口内的计数。"""

    def __init__(self) -> None:
        self._stats: dict[str, SpeakerStat] = {}

    def observe(self, node: MessageNode) -> None:
        stat = self._stats.get(node.sender_id)
        if stat is None:
            stat = SpeakerStat(user_id=node.sender_id, name=node.sender_name)
            self._stats[node.sender_id] = stat
        stat.name = node.sender_name
        stat.count += 1
        stat.last_at = node.at
        normalized = normalize_text(node.text)
        if normalized:
            stat.texts[normalized] += 1

    def stats(
        self,
        since_sec: float = DEFAULT_WINDOW_SEC,
        now: float | None = None,
    ) -> list[SpeakerStat]:
        current = time.monotonic() if now is None else now
        floor = current - since_sec
        rows = [stat for stat in self._stats.values() if stat.last_at >= floor]
        rows.sort(key=lambda stat: (stat.count, stat.last_at), reverse=True)
        return rows

    def count_of(self, user_id: str, text: str | None = None) -> int:
        stat = self._stats.get(user_id)
        if stat is None:
            return 0
        if text is None:
            return stat.count
        return stat.texts.get(normalize_text(text), 0)

    def name_of(self, user_id: str) -> str | None:
        stat = self._stats.get(user_id)
        return stat.name if stat is not None else None

    def who_said(self, text: str) -> list[tuple[str, int]]:
        normalized = normalize_text(text)
        rows = [
            (stat.name, count)
            for stat in self._stats.values()
            if (count := stat.texts.get(normalized, 0)) > 0
        ]
        rows.sort(key=lambda row: row[1], reverse=True)
        return rows


class GroupConversation:
    """单个群的会话图：消息环形缓冲 + 账本 + bot 发言标记。"""

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES) -> None:
        self.nodes: deque[MessageNode] = deque(maxlen=max_messages)
        self.ledger = SpeakerLedger()
        self.bot_id: str | None = None
        self.last_bot_at = 0.0
        self.last_bot_text = ""
        self.bot_mids: set[str] = set()

    def observe(self, node: MessageNode) -> None:
        self.nodes.append(node)
        self.ledger.observe(node)

    def note_bot(self, text: str, *, at: float, message_id: str | None = None) -> None:
        """记录 bot 自己刚发的话；不进入账本，只用于「上一句是谁说的」。"""
        self.last_bot_at = at
        self.last_bot_text = text
        if message_id:
            self.bot_mids.add(message_id)

    def history(self) -> list[MessageNode]:
        return list(self.nodes)

    def prev_speaker(self, node: MessageNode) -> tuple[str, str]:
        """返回 (openid, 显示名)；bot 用 ``BOT_TARGET`` 表示。"""
        others = [item for item in self.nodes if item.mid != node.mid]
        if not others:
            return (BOT_TARGET, "bot") if self.last_bot_at > 0 else ("", "")
        last = others[-1]
        if self.last_bot_at > last.at:
            return BOT_TARGET, "bot"
        return last.sender_id, last.sender_name

    def resolve(
        self,
        node: MessageNode,
        *,
        bot_names: tuple[str, ...] = DEFAULT_BOT_NAMES,
        window: int = 10,
    ) -> Addressee:
        recent = [item for item in list(self.nodes)[-window:] if item.mid != node.mid]
        candidates: list[tuple[float, int, str, tuple[str, ...]]] = []

        if node.mention_bot:
            candidates.append((1.0, 0, BOT_TARGET, ("@bot",)))

        if node.reply_to and node.reply_to in self.bot_mids:
            candidates.append((0.92, 1, BOT_TARGET, ("引用 bot",)))

        keyword = find_keyword(node.text, tuple(bot_names))
        if keyword and fast_kind(node.text, keyword) is KeywordKind.CALL:
            candidates.append((0.90, 2, BOT_TARGET, (f"名字 {keyword}",)))

        bot_prev = self.prev_speaker(node)[0] == BOT_TARGET
        if bot_prev and any(word in node.text for word in _SECOND_PERSON):
            candidates.append((0.75, 4, BOT_TARGET, ("「你」指 bot",)))
        if bot_prev and node.text.strip().endswith(_QUESTION_ENDINGS):
            candidates.append((0.60, 5, BOT_TARGET, ("bot 后的疑问",)))

        others = [uid for uid in node.mentions if uid != self.bot_id]
        if others:
            candidates.append((1.0, 3, others[0], ("@ 某人",)))

        partner = _alternating_partner(node, recent, self.bot_id)
        if partner:
            candidates.append((0.80, 6, partner, ("双人对话",)))

        if not candidates:
            return Addressee(GROUP_TARGET, 0.20, ("无明确指向",))
        score, _, target, evidence = max(candidates, key=lambda row: (row[0], -row[1]))
        return Addressee(target, score, evidence)

    def render(  # noqa: PLR0913
        self,
        node: MessageNode,
        addressee: Addressee,
        *,
        now: float,
        window_sec: float = DEFAULT_WINDOW_SEC,
        max_speakers: int = DEFAULT_MAX_SPEAKERS,
        max_phrases: int = DEFAULT_MAX_PHRASES,
        block_tokens: int = DEFAULT_BLOCK_TOKENS,
    ) -> str:
        stats = [
            stat
            for stat in self.ledger.stats(window_sec, now)
            if stat.user_id != self.bot_id
        ]
        who = self.prev_speaker(node)[1] or "没人"
        target = self._target_name(addressee.target)
        evidence = "、".join(addressee.evidence) or "无"
        speakers = _speaker_summary(stats, max_speakers, max_phrases)
        head = "[群聊状态] 最近 10 分钟：" + speakers
        text = (
            head
            + "；上一句是 "
            + who
            + " 说的；本条指向："
            + target
            + "（依据："
            + evidence
            + "）"
        )
        if len(text) <= block_tokens * 3:
            return text
        plain = "、".join(
            f"{stat.name}{_TIMES}{stat.count}" for stat in stats[:max_speakers]
        )
        return (
            "[群聊状态] 最近 10 分钟："
            + (plain or "暂无")
            + "；上一句是 "
            + who
            + " 说的；本条指向："
            + target
        )

    def _target_name(self, target: str) -> str:
        if target == BOT_TARGET:
            return "bot"
        if target == GROUP_TARGET:
            return "群里"
        return self.ledger.name_of(target) or "某人"


def _speaker_summary(
    stats: list[SpeakerStat], max_speakers: int, max_phrases: int
) -> str:
    rows = [_format_speaker(stat, max_phrases) for stat in stats[:max_speakers]]
    return "、".join(rows) if rows else "暂无"


def _format_speaker(stat: SpeakerStat, max_phrases: int) -> str:
    phrases = [
        (text, count)
        for text, count in stat.texts.most_common(max_phrases)
        if len(text) <= _MAX_PHRASE_CHARS
    ]
    if not phrases:
        return f"{stat.name}{_TIMES}{stat.count}"
    detail = "、".join(f"{text}{_TIMES}{count}" for text, count in phrases)
    return f"{stat.name}{_TIMES}{stat.count}（{detail}）"


def _alternating_partner(
    node: MessageNode,
    recent: list[MessageNode],
    bot_id: str | None,
) -> str | None:
    if len(recent) < _MIN_PAIR_CHARS:
        return None
    last, before = recent[-1], recent[-2]
    if last.sender_id in (node.sender_id, bot_id):
        return None
    if before.sender_id != node.sender_id:
        return None
    return last.sender_id


class ConversationManager:
    """按群维护 ``GroupConversation``，LRU 淘汰。"""

    def __init__(self, max_groups: int = MAX_GROUPS) -> None:
        self._groups: OrderedDict[str, GroupConversation] = OrderedDict()
        self._max_groups = max_groups
        self.bot_id = ""

    def bind_bot(self, bot_id: str) -> None:
        self.bot_id = str(bot_id)

    def _get(self, group_key: str) -> GroupConversation:
        conversation = self._groups.pop(group_key, None)
        if conversation is None:
            conversation = GroupConversation()
        conversation.bot_id = conversation.bot_id or self.bot_id
        self._groups[group_key] = conversation
        while len(self._groups) > self._max_groups:
            self._groups.popitem(last=False)
        return conversation

    def observe(  # noqa: PLR0913
        self,
        *,
        group_key: str,
        user_key: str,
        name: str,
        text: str,
        message_id: str,
        mentions: tuple[str, ...] = (),
        mention_bot: bool = False,
        at: float | None = None,
    ) -> None:
        conversation = self._get(group_key)
        conversation.observe(
            MessageNode(
                mid=message_id,
                sender_id=user_key,
                sender_name=name or UNKNOWN_NAME,
                text=text or "",
                at=time.monotonic() if at is None else at,
                mentions=mentions,
                mention_bot=mention_bot,
                tokens=topic_tokens(text or ""),
            )
        )

    def note_bot(
        self,
        group_key: str,
        text: str,
        *,
        message_id: str | None = None,
        at: float | None = None,
    ) -> None:
        conversation = self._groups.get(group_key)
        if conversation is None:
            return
        conversation.note_bot(
            text,
            at=time.monotonic() if at is None else at,
            message_id=message_id,
        )

    def count_of(
        self, group_key: str, user_key: str, text: str | None = None
    ) -> int:
        conversation = self._groups.get(group_key)
        if conversation is None:
            return 0
        return conversation.ledger.count_of(user_key, text)

    def addressee(
        self,
        group_key: str,
        *,
        bot_names: tuple[str, ...] = DEFAULT_BOT_NAMES,
    ) -> Addressee | None:
        conversation = self._groups.get(group_key)
        if conversation is None or not conversation.nodes:
            return None
        return conversation.resolve(conversation.nodes[-1], bot_names=bot_names)

    def block(  # noqa: PLR0913
        self,
        group_key: str,
        *,
        bot_names: tuple[str, ...] = DEFAULT_BOT_NAMES,
        window_sec: float = DEFAULT_WINDOW_SEC,
        max_speakers: int = DEFAULT_MAX_SPEAKERS,
        max_phrases: int = DEFAULT_MAX_PHRASES,
        block_tokens: int = DEFAULT_BLOCK_TOKENS,
        now: float | None = None,
    ) -> str:
        conversation = self._groups.get(group_key)
        if conversation is None or not conversation.nodes:
            return ""
        node = conversation.nodes[-1]
        addressee = conversation.resolve(node, bot_names=bot_names)
        return conversation.render(
            node,
            addressee,
            now=time.monotonic() if now is None else now,
            window_sec=window_sec,
            max_speakers=max_speakers,
            max_phrases=max_phrases,
            block_tokens=block_tokens,
        )

    def reset(self, group_key: str | None = None) -> None:
        if group_key is None:
            self._groups.clear()
        else:
            self._groups.pop(group_key, None)


manager = ConversationManager()
