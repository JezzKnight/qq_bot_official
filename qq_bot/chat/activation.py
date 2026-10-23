"""自动接话的判断层：纯函数，输入是信号，输出可解释的 ``Decision``。

两层结构，刻意分开：

1. 否决（硬线）—— 命令、空消息、同一人刷屏、冷却中、当天额度用完、没有被动
   窗口：命中任一条直接不接，理由写进 ``reason``；
2. 打分（软线）——「这条消息有多像在跟 bot 说话」：引用别人、点到 bot 名字、
   和 bot 上一句有实词重叠、疑问句、第二人称，累计过阈值后再摇一次概率。

分开的好处是 INFO 日志能同时回答「为什么没接」和「差几分」：上线第一周靠这行
日志调 ``group_autotrigger_min_score``，而不是拍脑袋。本模块不做任何 I/O。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

WEIGHT_QUOTE = 3  # 引用了别人的消息：多半在接话
WEIGHT_BOT_NAME = 3  # 直接点到 bot 的名字：几乎等于点名
WEIGHT_OVERLAP = 2  # 和 bot 上一句有实词重叠：同一话题
WEIGHT_QUESTION = 1  # 疑问句：留了接话的口子
WEIGHT_SECOND_PERSON = 1  # 有「你」：可能在对 bot 说
WEIGHT_LONG_SILENCE = 1  # bot 很久没说话：插一句更像人的时机
LONG_SILENCE_MINUTES = 10.0  # 超过 10 分钟算「很久」
SENDER_STREAK_LIMIT = 3  # 同一人 60 秒内连发到这个数就不接

SECOND_PERSON_WORDS = ("你", "你们", "咱")

_QUESTION_RE = re.compile(
    r"[?？]|吗|呢|怎么|为啥|为什么|是不是|能不能|有没有|什么|如何|多少|咋"
)


@dataclass(frozen=True)
class Decision:
    """一次接话判定的结果；``reason`` 直接进日志，便于调阈值。"""

    allowed: bool
    score: int
    reason: str


def looks_like_question(text: str) -> bool:
    """粗判疑问句：问号或常见疑问词。宁松勿紧，它只值 1 分。"""
    return bool(_QUESTION_RE.search(text or ""))


def decide(  # noqa: PLR0913 —— 判定必须一次拿齐全部信号，包成对象反而让测试更难构造
    text: str,
    *,
    is_command: bool,
    has_media: bool,
    quoted_someone: bool,
    mentions_bot_name: bool,
    is_question: bool,
    has_second_person: bool,
    overlaps_last_bot_msg: bool,
    minutes_since_bot_spoke: float | None,
    same_sender_streak: int,
    cooldown_ok: bool,
    daily_budget_ok: bool,
    window_available: bool,
    probability: float,
    min_score: int,
    rng: Callable[[], float] = random.random,
) -> Decision:
    """打分 + 否决 + 概率，三步走完返回可解释的决策。"""
    score = _score(
        quoted_someone=quoted_someone,
        mentions_bot_name=mentions_bot_name,
        overlaps_last_bot_msg=overlaps_last_bot_msg,
        is_question=is_question,
        has_second_person=has_second_person,
        minutes_since_bot_spoke=minutes_since_bot_spoke,
    )
    veto = _veto_reason(
        text,
        is_command=is_command,
        has_media=has_media,
        same_sender_streak=same_sender_streak,
        cooldown_ok=cooldown_ok,
        daily_budget_ok=daily_budget_ok,
        window_available=window_available,
    )
    if veto is not None:
        return Decision(allowed=False, score=score, reason=f"veto:{veto} score={score}")
    if score < min_score:
        return Decision(
            allowed=False,
            score=score,
            reason=f"score={score} below min={min_score}",
        )
    if rng() >= probability:
        return Decision(
            allowed=False,
            score=score,
            reason=f"score={score} probability miss p={probability}",
        )
    return Decision(
        allowed=True,
        score=score,
        reason=f"score={score} pass p={probability}",
    )


def _score(  # noqa: PLR0913 —— 打分项就是 6 个独立信号
    *,
    quoted_someone: bool,
    mentions_bot_name: bool,
    overlaps_last_bot_msg: bool,
    is_question: bool,
    has_second_person: bool,
    minutes_since_bot_spoke: float | None,
) -> int:
    score = 0
    if quoted_someone:
        score += WEIGHT_QUOTE
    if mentions_bot_name:
        score += WEIGHT_BOT_NAME
    if overlaps_last_bot_msg:
        score += WEIGHT_OVERLAP
    if is_question:
        score += WEIGHT_QUESTION
    if has_second_person:
        score += WEIGHT_SECOND_PERSON
    silent_for = minutes_since_bot_spoke
    if silent_for is not None and silent_for > LONG_SILENCE_MINUTES:
        score += WEIGHT_LONG_SILENCE
    return score


def _veto_reason(  # noqa: PLR0913, PLR0911 —— 否决就是逐条硬条件，一条一个 return
    text: str,
    *,
    is_command: bool,
    has_media: bool,
    same_sender_streak: int,
    cooldown_ok: bool,
    daily_budget_ok: bool,
    window_available: bool,
) -> str | None:
    stripped = (text or "").strip()
    if is_command:
        return "command"
    if not stripped:
        return "media_only" if has_media else "empty"
    if same_sender_streak >= SENDER_STREAK_LIMIT:
        return "sender_streak"
    if not cooldown_ok:
        return "cooldown"
    if not daily_budget_ok:
        return "daily_budget"
    if not window_available:
        return "no_window"
    return None
