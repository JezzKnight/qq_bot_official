"""回复文本的分段与发送节奏（官方 QQ 被动回复上限 5 次）。

三层职责：

1. ``split_for_reply`` —— 把模型输出切成"像 QQ 连发"的短消息：
   空行优先（模型显式分段），单段过长再按 句末 → 逗号 → 空格 三级断点切，
   硬切时避开数字/英文单词内部（``500N``、``1612N``、链接）。
2. ``typing_delay`` —— 按字数估算"打完这句话"要多久，作为下一条消息的停顿。
3. ``send_reply_chunks`` —— 首条立即发（群聊带 @），其后每条前停顿。

分段的目的是"一次说几条"，停顿的目的是"不是同时到达"；两者都由代码保证，
不再依赖模型自己在正文里写空行。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_bot.platform.ports import ReplyTarget

logger = logging.getLogger(__name__)

MAX_REPLY_PARTS = 4  # 官方被动回复 5 次，保守取 4，留出表情包的额度
MAX_CHUNK_CHARS = 30  # 单条消息超过该字数就切条（chatluna-character 同口径）
TYPE_DELAY_BASE = 0.35  # 每条消息的"抬手"时间（秒）
TYPE_DELAY_PER_CHAR = 0.07  # 每个字的打字时间（秒/字）
TYPE_DELAY_CAP = 1.8  # 单条最长停顿，避免 4 段拖成十秒

# 断点优先级：句末 → 分句 → 空格（我们的风格把逗号换成空格，空格才是主力断点）
_SENTENCE_END = "。！？…\n!?"
_CLAUSE_END = "，,;；、"
_SPACE = " "
# 这些单字放条尾会把一句话读成半句（"…的 7.2.9 是" 断在"是"后面就废了）
_DANGLING = frozenset("是的了就还也和跟在有把被给让从对")


def split_for_reply(
    text: str,
    *,
    max_parts: int = MAX_REPLY_PARTS,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[str]:
    """把回复切成若干条消息；超过 ``max_parts`` 时把尾部合并，避免撞发送上限。"""
    paragraphs = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    if not paragraphs:
        return [""]
    chunks = [piece for para in paragraphs for piece in _split_long(para, max_chars)]
    if len(chunks) <= max_parts:
        return chunks
    head = chunks[: max_parts - 1]
    head.append("\n".join(chunks[max_parts - 1 :]))
    return head


def _split_long(text: str, max_chars: int) -> list[str]:
    """把一段超长文本按语义断点切开；``max_chars <= 0`` 表示不切。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > max_chars:
        cut = _find_cut(rest, max_chars)
        piece = rest[:cut].strip()
        if not piece:  # 断点落在开头（极端情况）：退回硬切，保证前进
            cut = max_chars
            piece = rest[:cut].strip()
        pieces.append(piece)
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces or [text]


def _find_cut(text: str, limit: int) -> int:
    """在 ``text[:limit]`` 里找一个"像人换口气"的断点，返回切分位置。"""
    window = text[:limit]
    for group in (_SENTENCE_END, _CLAUSE_END):
        for i in range(len(window) - 1, 0, -1):
            if window[i] in group:
                return i + 1
    for i in range(len(window) - 1, 0, -1):
        if window[i] in _SPACE:
            return _avoid_dangling(text, i + 1)
    return _keep_word(text, limit)


def _avoid_dangling(text: str, cut: int) -> int:
    """空格断点别落在"是/的/了"这类单字后面，否则前半句读起来是断的。

    "…的 7.2.9 是 | 500N 那是…" 读着别扭，退一个空格变成 "…的 7.2.9 | 是 500N 那是…"。
    """
    while 0 < cut <= len(text):
        head = text[:cut].rstrip()
        if not head or head[-1] not in _DANGLING:
            return cut
        prev = text.rfind(" ", 0, len(head) - 1)
        if prev <= 0:
            return cut
        cut = prev + 1
    return cut


def _keep_word(text: str, limit: int) -> int:
    """硬切时不要切在单词/数字内部（500N、GB 7258、URL 都会被劈开）。"""
    cut = limit
    while 0 < cut < len(text) and _is_word(text[cut - 1]) and _is_word(text[cut]):
        cut -= 1
    return cut or limit


def _is_word(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "._/-%")


def typing_delay(text: str) -> float:
    """按字数估算"打完这句话"需要的停顿（秒），供分段发送使用。"""
    if not text.strip():
        return 0.0
    return min(TYPE_DELAY_CAP, TYPE_DELAY_BASE + TYPE_DELAY_PER_CHAR * len(text))


async def send_reply_chunks(
    reply: ReplyTarget,
    chunks: list[str],
    *,
    mention_user_key: str | None = None,
    simulate_typing: bool = True,
    fallback_to_text: bool = False,
) -> None:
    """逐段发送：首段立即（群聊走被动回复带 @），其后按打字时间停顿。

    ``mention_user_key`` 为 None 时不带 @（私聊）；
    ``fallback_to_text`` 为 True 时，首段 @ 发送失败会退回普通消息（后台视频回复）。
    """
    pending = [chunk for chunk in chunks if chunk.strip()]
    for idx, chunk in enumerate(pending):
        if idx == 0:
            await _send_first(
                reply,
                chunk,
                mention_user_key=mention_user_key,
                fallback_to_text=fallback_to_text,
            )
            continue
        if simulate_typing:
            await asyncio.sleep(typing_delay(chunk))
        await reply.text(chunk)


async def _send_first(
    reply: ReplyTarget,
    chunk: str,
    *,
    mention_user_key: str | None,
    fallback_to_text: bool,
) -> None:
    if mention_user_key is None:
        await reply.text(chunk)
        return
    try:
        await reply.mention(mention_user_key, chunk)
    except Exception:
        if not fallback_to_text:
            raise
        logger.warning("首段 @ 发送失败，改走普通消息", exc_info=True)
        await reply.text(chunk)
