"""群聊会话图：账本计数、指向解析、[群聊状态] 渲染。"""

from __future__ import annotations

import time

import pytest

from qq_bot.chat import conversation
from qq_bot.chat.conversation import (
    ConversationManager,
    GroupConversation,
    MessageNode,
    normalize_text,
    parse_bot_names,
    topic_tokens,
)


def node(  # noqa: PLR0913
    mid: str,
    sender: str,
    text: str,
    *,
    at: float = 100.0,
    name: str = "",
    mentions: tuple[str, ...] = (),
    mention_bot: bool = False,
) -> MessageNode:
    return MessageNode(
        mid=mid,
        sender_id=sender,
        sender_name=name or sender,
        text=text,
        at=at,
        mentions=mentions,
        mention_bot=mention_bot,
        tokens=topic_tokens(text),
    )


def test_ledger_counts_per_user() -> None:
    conv = GroupConversation()
    for index in range(3):
        conv.observe(node(f"m{index}", "u1", "fw", at=100.0 + index))

    assert conv.ledger.count_of("u1", "fw") == 3
    assert conv.ledger.count_of("u1") == 3


def test_ledger_does_not_merge_users() -> None:
    conv = GroupConversation()
    conv.observe(node("m1", "a", "fw", at=100.0))
    conv.observe(node("m2", "a", "fw", at=101.0))
    conv.observe(node("m3", "b", "废物", at=102.0))

    assert conv.ledger.count_of("a", "fw") == 2
    assert conv.ledger.count_of("b", "废物") == 1
    assert conv.ledger.count_of("b", "fw") == 0


def test_normalize_merges_width_and_case() -> None:
    conv = GroupConversation()
    conv.observe(node("m1", "u1", "FW", at=100.0))
    conv.observe(node("m2", "u1", "\uff46\uff57", at=101.0))

    assert normalize_text("\uff26 \uff37") == "f w"
    assert conv.ledger.count_of("u1", "fw") == 2


def test_mention_bot_wins() -> None:
    conv = GroupConversation()
    conv.bot_id = "bot"
    conv.observe(
        node("m1", "u1", "fw", mentions=("bot",), mention_bot=True)
    )

    addressee = conv.resolve(conv.nodes[-1])
    assert addressee.target == "bot"
    assert addressee.score == 1.0
    assert addressee.evidence == ("@bot",)


def test_name_call_points_to_bot() -> None:
    conv = GroupConversation()
    conv.bot_id = "bot"
    conv.observe(node("m1", "u1", "大肥鱼 在吗"))

    addressee = conv.resolve(conv.nodes[-1])
    assert addressee.target == "bot"
    assert addressee.score == 0.9


def test_second_person_after_bot() -> None:
    conv = GroupConversation()
    conv.bot_id = "bot"
    conv.observe(node("m1", "u1", "今天好热", at=100.0))
    conv.note_bot("确实", at=100.5)
    conv.observe(node("m2", "u2", "你觉得呢", at=101.0))

    addressee = conv.resolve(conv.nodes[-1])
    assert addressee.target == "bot"
    assert addressee.score == 0.75


def test_mention_other_points_to_user() -> None:
    conv = GroupConversation()
    conv.bot_id = "bot"
    conv.observe(node("m1", "u1", "你昨天那把太菜了", mentions=("u2",)))

    addressee = conv.resolve(conv.nodes[-1])
    assert addressee.target == "u2"
    assert addressee.score == 1.0


def test_alternating_pair() -> None:
    conv = GroupConversation()
    conv.bot_id = "bot"
    conv.observe(node("m1", "a", "在吗", at=100.0))
    conv.observe(node("m2", "b", "在", at=101.0))
    conv.observe(node("m3", "a", "打游戏不", at=102.0))
    conv.observe(node("m4", "b", "来", at=103.0))

    addressee = conv.resolve(conv.nodes[-1])
    assert addressee.target == "a"
    assert addressee.score == 0.8


def test_no_signal_is_group() -> None:
    conv = GroupConversation()
    conv.bot_id = "bot"
    conv.observe(node("m1", "u1", "今天天气不错"))

    addressee = conv.resolve(conv.nodes[-1])
    assert addressee.target == "group"
    assert addressee.score == 0.2


def test_empty_conversation_is_safe() -> None:
    manager = ConversationManager()
    manager.note_bot("missing", "没人", at=time.monotonic())

    assert manager.addressee("missing") is None


def test_manager_evicts_oldest_group() -> None:
    manager = ConversationManager(max_groups=2)
    for group in ("g1", "g2", "g3"):
        manager.observe(
            group_key=group,
            user_key="u1",
            name="小明",
            text="hi",
            message_id=f"m-{group}",
            at=100.0,
        )

    assert manager.addressee("g1") is None
    assert manager.addressee("g3") is not None


def test_replay_insult_scene() -> None:
    manager = ConversationManager()
    manager.bind_bot("bot")
    script = [
        ("m1", "a", "隐身人", "fw"),
        ("m2", "a", "隐身人", "fw"),
        ("m3", "a", "隐身人", "fw"),
        ("m4", "b", "AMelax", "废物"),
        ("m5", "b", "AMelax", "废物"),
    ]
    for index, (mid, uid, name, text) in enumerate(script):
        manager.observe(
            group_key="g1",
            user_key=uid,
            name=name,
            text=text,
            message_id=mid,
            mentions=("bot",),
            mention_bot=True,
            at=100.0 + index,
        )

    assert manager.count_of("g1", "a", "fw") == 3
    assert manager.count_of("g1", "b", "废物") == 2
    addressee = manager.addressee("g1")
    assert addressee is not None
    assert addressee.target == "bot"


def test_parse_bot_names() -> None:
    assert parse_bot_names("") == conversation.DEFAULT_BOT_NAMES
    assert parse_bot_names("大肥鱼, 鱼鱼") == ("大肥鱼", "鱼鱼")


def test_topic_tokens_drops_punctuation() -> None:
    tokens = topic_tokens("大肥鱼，在吗？")
    assert "大肥" in tokens
    assert "，" not in tokens

    with pytest.raises(AttributeError):
        tokens.add("x")


def test_observe_skips_duplicate_mid() -> None:
    conv = GroupConversation()
    conv.observe(node("m1", "U1", "在吗"))
    conv.observe(node("m1", "U1", "在吗"))
    assert len(conv.history()) == 1
    assert conv.ledger.count_of("U1") == 1


def test_rebuild_restores_window_and_ledger() -> None:
    from qq_bot.ai.types import ChatMessage

    conv = GroupConversation()
    conv.rebuild(
        [
            ChatMessage(
                role="user", sender_name="小明", sender_id="U1", content="三斤的鲤鱼"
            ),
            ChatMessage(role="assistant", content="牛啊"),
            ChatMessage(
                role="user", sender_name="阿强", sender_id="U2", content="我五斤二两"
            ),
        ]
    )
    nodes = conv.history()
    assert [n.sender_name for n in nodes] == ["小明", "阿强"]
    assert nodes[0].sender_id == "U1"
    assert conv.ledger.count_of("U1") == 1
    assert conv.last_bot_text == "牛啊"
    # 顺序是 小明 → 牛啊(bot) → 阿强，所以阿强的上一句是 bot
    assert conv.prev_speaker(nodes[-1]) == ("bot", "bot")


def test_rebuild_marks_bot_as_last_speaker() -> None:
    from qq_bot.ai.types import ChatMessage

    conv = GroupConversation()
    conv.rebuild(
        [
            ChatMessage(
                role="user", sender_name="小明", sender_id="U1", content="在吗"
            ),
            ChatMessage(role="assistant", content="在"),
        ]
    )
    conv.observe(node("m2", "U9", "来了", at=5.0))
    assert conv.prev_speaker(conv.history()[-1]) == ("bot", "bot")


def test_manager_tracks_rebuild_state() -> None:
    manager = ConversationManager()
    assert manager.needs_rebuild("G1")
    manager.mark_rebuilt("G1")
    assert not manager.needs_rebuild("G1")
    manager.reset()
    assert manager.needs_rebuild("G1")


def test_followup_question_targets_bot() -> None:
    conv = GroupConversation()
    conv.observe(node("m1", "U1", "大肥鱼在不在", at=100.0))
    conv.note_bot("在的", at=101.0)
    conv.observe(node("m2", "U1", "昨晚CS比赛什么结果", at=110.0))
    addressee = conv.resolve(conv.history()[-1], bot_names=("大肥鱼",))
    assert addressee.target == "bot"
    assert addressee.score == 0.65
    assert "追问" in addressee.evidence[0]


def test_followup_question_expires() -> None:
    conv = GroupConversation()
    conv.observe(node("m1", "U1", "大肥鱼在不在", at=100.0))
    conv.note_bot("在 咋了", at=101.0)
    conv.observe(node("m2", "U1", "昨晚CS比赛什么结果", at=500.0))
    addressee = conv.resolve(conv.history()[-1], bot_names=("大肥鱼",))
    assert addressee.target == "group"


def test_plain_reply_after_bot_is_not_followup() -> None:
    conv = GroupConversation()
    conv.observe(node("m1", "U1", "大肥鱼在不在", at=100.0))
    conv.note_bot("好的", at=101.0)
    conv.observe(node("m2", "U1", "我草里的", at=110.0))
    addressee = conv.resolve(conv.history()[-1], bot_names=("大肥鱼",))
    assert addressee.target == "group"


def test_reply_after_bot_question_targets_bot() -> None:
    conv = GroupConversation()
    conv.observe(node("m1", "U1", "大肥鱼在不在", at=100.0))
    conv.note_bot("在 咋了", at=101.0)
    conv.observe(node("m2", "U1", "帮我查一下昨天CS比赛的结果", at=110.0))
    addressee = conv.resolve(conv.history()[-1], bot_names=("大肥鱼",))
    assert addressee.target == "bot"
    assert addressee.score == 0.80
    assert "承接" in addressee.evidence[0]


def test_last_user_node_returns_recent_message() -> None:
    manager = ConversationManager()
    manager.observe(
        group_key="g1",
        user_key="U1",
        name="小明",
        text="帮我查一下昨天CS比赛的结果",
        message_id="m1",
        at=100.0,
    )
    node = manager.last_user_node("g1", "U1", now=110.0)
    assert node is not None
    assert node.text == "帮我查一下昨天CS比赛的结果"
    assert node.mid == "m1"
    assert manager.last_user_node("g1", "U1", now=300.0) is None
    assert manager.last_user_node("g1", "U2", now=110.0) is None


def test_latest_user_mid_tracks_newest_message() -> None:
    manager = ConversationManager()
    assert manager.latest_user_mid("g1") == ""
    manager.observe(
        group_key="g1", user_key="U1", name="小明", text="一",
        message_id="m1", at=100.0,
    )
    manager.observe(
        group_key="g1", user_key="U2", name="AMelax", text="二",
        message_id="m2", at=101.0,
    )
    assert manager.latest_user_mid("g1") == "m2"
