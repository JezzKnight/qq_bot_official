"""关键词触发的语境判定与过渡句生成（P2，规格 §6.9）。

名字出现时先走规则快路，剩下的歧义交给 flash 分类；分类失败一律当成
``HOMONYM``（宁可不动）。过渡句也由模型生成，失败回退兜底句。
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompts.loader import PromptLoader
from prompts.renderer import PromptRenderer
from qq_bot.ai.types import ChatMessage
from qq_bot.services.client_factory import get_client_for_model

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

CLASSIFY_MODEL = "deepseek-v4-flash"
CLASSIFY_TIMEOUT_SEC = 2.0
TRANSITION_MODEL = "deepseek-v4-flash"
TRANSITION_TIMEOUT_SEC = 2.5
TRANSITION_MAX_CHARS = 60
CLASSIFY_MAX_CHARS = 200
CONTEXT_MAX_CHARS = 120

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"
_loader = PromptLoader(_PROMPT_DIR)
_renderer = PromptRenderer()

# 名字后面跟这些词，基本是在说鱼/菜，不是叫 bot
_HOMONYM_AFTER = (
    "丸",
    "多少钱",
    "好吃",
    "怎么钓",
    "红烧",
    "清蒸",
    "干锅",
    "养殖",
)
# 名字前面是这些词，基本是在说「我家的/这条」那条鱼
_HOMONYM_BEFORE = (
    "我家的",
    "这条",
    "那条",
    "这尾",
    "买了",
    "吃",
    "钓",
    "养",
    "烧",
    "做",
    "卖",
)
_CALL_TAIL = re.compile(
    r"^(在吗|在不在|出来|出来一下|帮我|问你|喂|你好|哈喽|hi|hello)", re.IGNORECASE
)
_TRAILING = "。.!！?？~…，, \t"
_LEADING_QUOTES = "\"'“”「」『』 "
_PREFIX_RE = re.compile(r"^回复[:：]\s*")
_MENTION_RE = re.compile(r"@\S+\s*")


class KeywordKind(str, Enum):
    """名字出现的三种语境。"""

    CALL = "call"  # 在叫 bot
    ABOUT = "about"  # 在聊 bot
    HOMONYM = "homonym"  # 同名的鱼/菜/宠物/别人


def find_keyword(text: str, keywords: Iterable[str]) -> str | None:
    """返回文本里命中的第一个关键词（精确子串）。"""
    for keyword in keywords:
        if keyword and keyword in (text or ""):
            return keyword
    return None


def fast_kind(  # noqa: PLR0911
    text: str, keyword: str
) -> KeywordKind | None:
    """规则快路：只处理高置信度的情况，其余返回 ``None`` 交模型。"""
    value = (text or "").strip()
    if not value or not keyword:
        return None
    if value == keyword:
        return KeywordKind.CALL
    if value.startswith(keyword):
        tail = value[len(keyword) :].lstrip(_TRAILING)
        if not tail:
            return KeywordKind.CALL
        if _CALL_TAIL.match(tail):
            return KeywordKind.CALL
        if tail.startswith(_HOMONYM_AFTER):
            return KeywordKind.HOMONYM
        if "你" in tail:
            return KeywordKind.CALL
        return None
    index = value.find(keyword)
    if index > 0:
        before = value[:index].rstrip(_TRAILING)
        if before.endswith(_HOMONYM_BEFORE):
            return KeywordKind.HOMONYM
        # 「今天大肥鱼没说话」这类第三人称，交模型判断是 ABOUT 还是 HOMONYM
    return None


async def classify(
    text: str, keyword: str, *, timeout: float = CLASSIFY_TIMEOUT_SEC
) -> KeywordKind:
    """语境分类：规则优先，剩余歧义交 flash；任何失败都按 HOMONYM 处理。"""
    fast = fast_kind(text, keyword)
    if fast is not None:
        return fast
    try:
        client = await get_client_for_model(CLASSIFY_MODEL)
        messages = [
            ChatMessage(role="system", content=_classifier_prompt(keyword)),
            ChatMessage(
                role="user",
                content=(
                    f"<message>{(text or '').strip()[:CLASSIFY_MAX_CHARS]}</message>"
                ),
            ),
        ]
        response = await asyncio.wait_for(
            client.chat(
                messages=messages,
                model=CLASSIFY_MODEL,
                temperature=0.0,
                max_tokens=8,
                reasoning_effort="none",
            ),
            timeout=timeout,
        )
        label = (response.content or "").strip().upper()
        for kind in (KeywordKind.CALL, KeywordKind.ABOUT, KeywordKind.HOMONYM):
            if kind.name in label:
                return kind
        logger.info("keyword classify unexpected output %r", label[:20])
        return KeywordKind.HOMONYM  # noqa: TRY300
    except Exception:
        logger.warning("keyword classify failed, fail-closed to homonym", exc_info=True)
        return KeywordKind.HOMONYM


async def generate_transition(  # noqa: PLR0913 —— 生成过渡句需要这些上下文
    *,
    keyword: str,
    text: str,
    nick: str,
    group_window: Sequence[Any] = (),
    summary: str | None = None,
    timeout: float = TRANSITION_TIMEOUT_SEC,
) -> str | None:
    """按人设生成 ``ABOUT`` 的过渡句；失败返回 ``None``（调用方用兜底句）。"""
    try:
        template = _loader.load("chat/keyword_transition.md")
        prompt = _renderer.render(
            template,
            keyword=keyword,
            nick=nick or "有人",
            summary=(summary or "").strip() or "（暂无群摘要）",
            window=_format_window(group_window) or "（暂无群聊记录）",
            message=(text or "").strip()[:CLASSIFY_MAX_CHARS],
        )
        messages = [
            ChatMessage(
                role="system",
                content=f"{_system_prompt()}\n\n{prompt}",
            ),
            ChatMessage(role="user", content="（按系统指令，只输出你要说的话）"),
        ]
        client = await get_client_for_model(TRANSITION_MODEL)
        response = await asyncio.wait_for(
            client.chat(
                messages=messages,
                model=TRANSITION_MODEL,
                temperature=0.8,
                max_tokens=200,
                reasoning_effort="none",
            ),
            timeout=timeout,
        )
        return _clean_transition(response.content)
    except Exception:
        logger.warning("keyword transition failed, use fallback", exc_info=True)
        return None


def _classifier_prompt(keyword: str) -> str:
    return (
        f"你是语境分类器。群里的机器人叫「{keyword}」。\n"
        "判断下面这条消息里的名字是在：\n"
        "- CALL：叫机器人、对机器人说话、让机器人做事；\n"
        "- ABOUT：拿名字当谈资——评价、比较、夸、吐槽，机器人可以顺势接话；\n"
        "- HOMONYM：名字指真正的鱼/菜/宠物/商品，或另一个同名的人。\n"
        "判断要点：带「我的/我家的」但整句是在评价、比较、炫耀这个名字，算 ABOUT；\n"
        "出现做菜、吃、钓、养、价格、斤两、品种等实体语境，算 HOMONYM。\n"
        f"例子：\n"
        f"「{keyword} 在吗」→ CALL\n"
        f"「我觉得我的{keyword}更牛逼」→ ABOUT\n"
        f"「{keyword}今天真菜」→ ABOUT\n"
        f"「{keyword}丸好吃吗」→ HOMONYM\n"
        f"「今天钓了两条{keyword}」→ HOMONYM\n"
        "只输出 CALL、ABOUT、HOMONYM 其中一个，不要解释。"
    )


def _system_prompt() -> str:
    from prompts.service import prompt_service

    return prompt_service.get_system_prompt()


def _format_window(messages: Sequence[Any]) -> str:
    lines: list[str] = []
    for message in messages:
        if getattr(message, "role", "user") != "user":
            continue
        content = (getattr(message, "content", "") or "").strip().replace("\n", " ")
        if not content:
            continue
        name = getattr(message, "sender_name", None) or "群友"
        lines.append(f"{name}: {content[:CONTEXT_MAX_CHARS]}")
    return "\n".join(lines)


def _clean_transition(content: str | None) -> str | None:
    value = (content or "").strip()
    if not value:
        return None
    value = value.splitlines()[0].strip()
    value = value.strip(_LEADING_QUOTES)
    value = _PREFIX_RE.sub("", value)
    value = _MENTION_RE.sub("", value).strip()
    if len(value) > TRANSITION_MAX_CHARS:
        value = value[:TRANSITION_MAX_CHARS].rstrip(_TRAILING)
    return value or None
