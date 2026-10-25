"""群聊参与策略：把明确的「回 / 过」从 judge 手里拿回来。

规则只吃 ``Addressee`` 与「话题是否相关」，不做概率：
- 明确叫 bot -> 必回（交给 presence 的 force 通道，仍受静默/日上限约束）；
- 明确指向某个用户 -> 过（别人的对话不插嘴）；
- 指向群里且话题无关 -> 过；
- 其余模糊情况 -> 交回 presence（judge 或 0-1 抽奖）。
"""

from __future__ import annotations

from dataclasses import dataclass

from qq_bot.chat.conversation import BOT_TARGET, GROUP_TARGET, Addressee

FORCE_REPLY_SCORE = 0.9

ACTION_REPLY = "reply"
ACTION_PASS = "pass"
ACTION_PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """``action`` 只取 reply / pass / presence 三值。"""

    action: str
    reason: str


def decide(
    addressee: Addressee | None,
    *,
    relevant: bool,
    force_score: float = FORCE_REPLY_SCORE,
) -> PolicyDecision:
    """按会话图的指向给出策略；拿不准就交回 presence。"""
    if addressee is None:
        return PolicyDecision(ACTION_PRESENCE, "no_context")
    if addressee.target == BOT_TARGET:
        if addressee.score >= force_score:
            return PolicyDecision(ACTION_REPLY, "target_bot")
        # 「你」指 bot（0.75）或 bot 后的疑问（0.6）：模糊，交给 judge
        return PolicyDecision(ACTION_PRESENCE, "bot_gray")
    if addressee.target == GROUP_TARGET:
        if relevant:
            return PolicyDecision(ACTION_PRESENCE, "topic_relevant")
        return PolicyDecision(ACTION_PASS, "topic_irrelevant")
    # 指向某个具体用户：这条不是对 bot 说的，别插嘴
    return PolicyDecision(ACTION_PASS, "target_other_user")
