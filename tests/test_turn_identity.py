"""群历史消息的身份标签：只在拼 prompt 时加，不落库。"""

from __future__ import annotations

from qq_bot.ai.types import ChatMessage
from qq_bot.chat.turn import _annotate_history


def test_user_message_gets_identity_prefix() -> None:
    history = [ChatMessage(role="user", sender_name="小明", content="我钓了三斤")]
    annotated = _annotate_history(history)
    assert annotated[0].content == '<user identity name="小明"/>\n我钓了三斤'
    assert annotated[0].sender_name == "小明"


def test_missing_name_falls_back_to_member() -> None:
    history = [ChatMessage(role="user", content="在吗")]
    annotated = _annotate_history(history)
    assert annotated[0].content == '<user identity name="某成员"/>\n在吗'


def test_assistant_and_already_tagged_messages_untouched() -> None:
    tagged = '<user identity name="阿强"/>\n来了'
    history = [
        ChatMessage(role="assistant", content="牛啊"),
        ChatMessage(role="user", sender_name="阿强", content=tagged),
        ChatMessage(role="user", sender_name="小明", content=None),
    ]
    annotated = _annotate_history(history)
    assert annotated[0].content == "牛啊"
    assert annotated[1].content == tagged
    assert annotated[2].content is None
