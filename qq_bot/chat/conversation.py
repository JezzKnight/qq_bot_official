"""群聊会话图：消息缓冲、发言账本与指向解析。

维护每群最近若干条消息、按人聚合发言统计，并解析当前消息在跟谁说话。
不落库；进程启动后按群懒加载，从 ``messages`` 表回填最近窗口。
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

from qq_bot.chat.relevance import KeywordKind, fast_kind, find_keyword

if TYPE_CHECKING:
    from collections.abc import Sequence

    from qq_bot.ai.types import ChatMessage

MAX_GROUPS = 512
DEFAULT_WINDOW_SEC = 600.0
DEFAULT_MAX_MESSAGES = 30
DEFAULT_BOT_NAMES: tuple[str, ...] = ("大肥鱼",)
UNKNOWN_NAME = "有人"
BOT_TARGET = "bot"
GROUP_TARGET = "group"

_WHITESPACE = re.compile(r"\s+")
_SECOND_PERSON = ("你", "你们")
_QUESTION_ENDINGS = ("吗", "呢", "?", "？")
_MAX_PHRASE_CHARS = 16
_MIN_PAIR_CHARS = 2

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
        if any(item.mid == node.mid for item in self.nodes):
            return
        self.nodes.append(node)
        self.ledger.observe(node)

    def note_bot(self, text: str, *, at: float, message_id: str | None = None) -> None:
        """记录 bot 自己刚发的话；不进入账本，只用于「上一句是谁说的」。"""
        self.last_bot_at = at
        self.last_bot_text = text
        if message_id:
            self.bot_mids.add(message_id)

    def rebuild(self, rows: Sequence[ChatMessage]) -> None:
        """从数据库行重建窗口；assistant 行只恢复 bot 发言标记。"""
        self.nodes.clear()
        self.ledger = SpeakerLedger()
        self.bot_mids.clear()
        self.last_bot_at = 0.0
        self.last_bot_text = ""
        for index, row in enumerate(rows):
            at = float(index)
            content = row.content or ""
            if row.role == "assistant":
                self.note_bot(content, at=at)
                continue
            if row.role != "user" or not content:
                continue
            self.observe(
                MessageNode(
                    mid=row.external_id or f"db:{index}",
                    sender_id=row.sender_id or f"db-user:{index}",
                    sender_name=row.sender_name or UNKNOWN_NAME,
                    text=content,
                    at=at,
                    tokens=topic_tokens(content),
                )
            )

    def history(self) -> list[MessageNode]:
        return list(self.nodes)

    def topic_relevant(
        self,
        node: MessageNode,
        *,
        window: int = 5,
        min_overlap: int = 2,
    ) -> bool:
        """本条消息的话题是否还围着 bot 或最近几轮在转。"""
        if not node.tokens:
            return False
        if len(node.tokens & topic_tokens(self.last_bot_text)) >= min_overlap:
            return True
        recent = [item for item in list(self.nodes)[-window:] if item.mid != node.mid]
        if not recent:
            return False
        group_tokens = frozenset().union(*(item.tokens for item in recent))
        return len(node.tokens & group_tokens) >= min_overlap

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
        self._rebuilt: set[str] = set()
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

    def topic_relevant(
        self,
        group_key: str,
        *,
        window: int = 5,
        min_overlap: int = 2,
    ) -> bool:
        conversation = self._groups.get(group_key)
        if conversation is None or not conversation.nodes:
            return False
        return conversation.topic_relevant(
            conversation.nodes[-1], window=window, min_overlap=min_overlap
        )

    def needs_rebuild(self, group_key: str) -> bool:
        """本群是否还没从数据库回填过。"""
        return group_key not in self._rebuilt

    def mark_rebuilt(self, group_key: str) -> None:
        self._rebuilt.add(group_key)

    def rebuild(self, group_key: str, rows: Sequence[ChatMessage]) -> None:
        self._get(group_key).rebuild(rows)

    def reset(self) -> None:
        self._groups.clear()
        self._rebuilt.clear()

manager = ConversationManager()
