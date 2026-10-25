"""参与策略：明确指向直接回/过，模糊交回 presence。"""

from __future__ import annotations

from qq_bot.chat.conversation import Addressee
from qq_bot.chat.participation import (
    ACTION_PASS,
    ACTION_PRESENCE,
    ACTION_REPLY,
    decide,
)


def test_no_context_falls_back_to_presence() -> None:
    decision = decide(None, relevant=True)
    assert decision.action == ACTION_PRESENCE
    assert decision.reason == "no_context"


def test_target_bot_forces_reply() -> None:
    decision = decide(Addressee("bot", 1.0, ("@bot",)), relevant=False)
    assert decision.action == ACTION_REPLY
    assert decision.reason == "target_bot"


def test_second_person_bot_is_gray() -> None:
    decision = decide(Addressee("bot", 0.75, ("「你」指 bot",)), relevant=True)
    assert decision.action == ACTION_PRESENCE
    assert decision.reason == "bot_gray"


def test_other_user_is_skipped() -> None:
    decision = decide(Addressee("u2", 1.0, ("@ 某人",)), relevant=True)
    assert decision.action == ACTION_PASS
    assert decision.reason == "target_other_user"


def test_group_topic_relevant_keeps_lottery() -> None:
    decision = decide(Addressee("group", 0.2, ("无明确指向",)), relevant=True)
    assert decision.action == ACTION_PRESENCE
    assert decision.reason == "topic_relevant"


def test_group_topic_irrelevant_is_skipped() -> None:
    decision = decide(Addressee("group", 0.2, ("无明确指向",)), relevant=False)
    assert decision.action == ACTION_PASS
    assert decision.reason == "topic_irrelevant"


def test_force_score_threshold_is_configurable() -> None:
    loose = decide(Addressee("bot", 0.85, ("名字",)), relevant=False, force_score=0.8)
    assert loose.action == ACTION_REPLY
    strict = decide(
        Addressee("bot", 0.85, ("名字",)), relevant=False, force_score=0.95
    )
    assert strict.action == ACTION_PRESENCE
