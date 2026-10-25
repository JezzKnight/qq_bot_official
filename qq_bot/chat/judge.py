"""AWAKE 窗口内的三态判定：REPLY / PASS / SLEEP。

进入窗口后不再"每条必回"，而是先让一个便宜模型判断这条消息要不要接：

- REPLY：这条是在跟 bot 说，正常跑一轮；
- PASS：不接，但留在窗口里，等下一句；
- SLEEP：话题已经交给别人，关窗并进入唤醒冷却。

判定失败/超时一律 PASS：宁可少说一句，也不能因为一次网络抖动退场。
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING

from qq_bot.ai.types import ChatMessage
from qq_bot.services.client_factory import api_model_name, get_client_for_model

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

JUDGE_MODEL = "deepseek-v41-flash"
JUDGE_TIMEOUT_SEC = 1.5
JUDGE_MAX_CHARS = 200

_SYSTEM = (
    "你在一个 QQ 群里，刚接过话。现在判断下面这条群消息要不要接。\n"
    "只输出 REPLY、PASS、SLEEP 其中一个，不要解释。\n"
    "- REPLY：这条是在跟机器人说话、在接机器人上一句、或明显冲着机器人来；\n"
    "- PASS：不确定，或不是对机器人说的，但话题可能还会回来；\n"
    "- SLEEP：话题已经转到别处、用户在跟别人聊、对话已经不需要机器人了。\n"
    "默认 PASS；只有明确冲着机器人来才 REPLY；只有明确不再需要机器人才 SLEEP。\n"
    "有人让机器人帮忙做事（帮我查/搜一下/找一下/看看）也算冲着机器人来。\n"
    "注意：用户之间互相 @、回答别人、评论别人之间的对话、聊完全不相关的新话题，"
    "都不要 REPLY。\n"
    "例子：\n"
    "「大肥鱼 在吗」→ REPLY\n"
    "「帮我查一下昨天CS比赛的结果」→ REPLY\n"
    "「@小明 你昨天那把打野太菜了」→ PASS\n"
    "「他们俩聊游戏呢 我们换个话题」→ PASS\n"
    "「行了 没你事了」→ SLEEP\n"
)


class JudgeAction(str, Enum):
    """窗口内判定的三种结果。"""

    REPLY = "reply"
    PASS = "pass"
    SLEEP = "sleep"


def parse_action(raw: str | None) -> JudgeAction:
    """解析模型输出；无法识别时按 PASS 处理。"""
    label = (raw or "").strip().upper()
    for action in JudgeAction:
        if action.name in label:
            return action
    if label:
        logger.info("presence judge unexpected output %r", label[:20])
    return JudgeAction.PASS


def format_recent(recent: Sequence[dict], *, limit_chars: int = JUDGE_MAX_CHARS) -> str:
    """把最近几条群消息压成「名字: 内容」的纯文本。"""
    lines: list[str] = []
    for item in recent:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        who = "bot" if item.get("role") == "assistant" else (item.get("name") or "有人")
        lines.append(f"{who}: {content[:limit_chars]}")
    return "\n".join(lines)


def _user_block(
    text: str,
    speaker: str,
    recent: Sequence[dict],
    bot_last: str,
) -> str:
    parts = [
        "## 最近群聊",
        format_recent(recent) or "（暂无）",
        "",
        "## 机器人上一句",
        (bot_last or "（还没说过话）").strip()[:JUDGE_MAX_CHARS],
        "",
        "## 新消息",
        f"{speaker or '有人'}: {(text or '').strip()[:JUDGE_MAX_CHARS]}",
    ]
    return "\n".join(parts)


async def judge_in_window(
    *,
    text: str,
    speaker: str = "",
    recent: Sequence[dict] = (),
    bot_last: str = "",
    timeout: float = JUDGE_TIMEOUT_SEC,
) -> JudgeAction:
    """判定一条 AWAKE 消息；任何异常都退化成 PASS（留在窗口）。"""
    try:
        client = await get_client_for_model(JUDGE_MODEL)
        response = await asyncio.wait_for(
            client.chat(
                messages=[
                    ChatMessage(role="system", content=_SYSTEM),
                    ChatMessage(
                        role="user",
                        content=_user_block(text, speaker, recent, bot_last),
                    ),
                ],
                model=api_model_name(JUDGE_MODEL),
                temperature=0.0,
                max_tokens=8,
                reasoning_effort="none",
            ),
            timeout=timeout,
        )
        return parse_action(response.content)
    except Exception:
        logger.warning("presence judge failed, fail-open to pass", exc_info=True)
        return JudgeAction.PASS
