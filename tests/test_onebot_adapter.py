"""OneBot 适配器：事件归一化与回复通道。"""

from __future__ import annotations

from dataclasses import dataclass, field

from qq_bot.platform.onebot.incoming import ONEBOT_CAPS, from_event


@dataclass
class FakeSeg:
    type: str
    data: dict = field(default_factory=dict)


class FakeMessage(list):
    def extract_plain_text(self) -> str:
        return "".join(
            str(s.data.get("text", "")) for s in self if s.type == "text"
        )


@dataclass
class FakeSender:
    card: str = ""
    nickname: str = ""


@dataclass
class FakeReply:
    sender: FakeSender
    message: FakeMessage


class FakeEvent:
    def __init__(
        self,
        *,
        user_id: int = 10001,
        group_id: int | None = None,
        message: FakeMessage | None = None,
        reply: FakeReply | None = None,
        message_id: int = 42,
    ) -> None:
        self.user_id = user_id
        self.group_id = group_id
        self.message_id = message_id
        self.reply = reply
        self.sender = FakeSender(card="", nickname="小明")
        self._message = message or FakeMessage()

    def get_message(self) -> FakeMessage:
        return self._message

    def get_plaintext(self) -> str:
        return self._message.extract_plain_text()


class FakeReplyTarget:
    max_parts = 4

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def text(self, content: str) -> None:
        self.sent.append(("text", content))

    async def image(self, data: bytes, name: str = "") -> None:  # noqa: ARG002
        self.sent.append(("image", data))

    async def finish(self) -> None:
        self.sent.append(("finish", None))


def test_group_event_is_normalized() -> None:
    event = FakeEvent(
        group_id=795245301,
        message=FakeMessage([FakeSeg("text", {"text": " 你好 "})]),
    )
    incoming = from_event(event, reply=FakeReplyTarget(), self_key="999")
    assert incoming.platform == "onebot"
    assert incoming.user_key == "10001"
    assert incoming.group_key == "795245301"
    assert incoming.is_group is True
    assert incoming.text == "你好"
    assert incoming.display_name == "小明"
    assert incoming.message_id == "42"
    assert incoming.self_key == "999"
    assert incoming.caps == ONEBOT_CAPS


def test_private_event_has_no_group() -> None:
    event = FakeEvent(message=FakeMessage([FakeSeg("text", {"text": "在吗"})]))
    incoming = from_event(event, reply=FakeReplyTarget())
    assert incoming.group_key is None
    assert incoming.is_group is False


def test_quoted_message_becomes_prefix() -> None:
    reply = FakeReply(
        sender=FakeSender(nickname="小红"),
        message=FakeMessage(
            [FakeSeg("text", {"text": "原话"}), FakeSeg("image", {"url": "http://x/1.jpg"})]
        ),
    )
    event = FakeEvent(
        message=FakeMessage([FakeSeg("text", {"text": "这是啥"})]), reply=reply
    )
    incoming = from_event(event, reply=FakeReplyTarget())
    assert incoming.text.startswith('[用户引用了 小红 的消息："原话"，[1 张图片]]')
    assert incoming.text.endswith("这是啥")


def test_media_segments_and_text_video_links() -> None:
    event = FakeEvent(
        message=FakeMessage(
            [
                FakeSeg("image", {"url": "http://x/a.jpg"}),
                FakeSeg("video", {"url": "http://x/b.mp4"}),
                FakeSeg("text", {"text": "看这个 http://x/c.mp4"}),
            ]
        )
    )
    incoming = from_event(event, reply=FakeReplyTarget())
    refs = [(m.kind, m.url) for m in incoming.images]
    assert ("image", "http://x/a.jpg") in refs
    assert ("video", "http://x/b.mp4") in refs
    assert ("video", "http://x/c.mp4") in refs


async def test_reply_target_methods() -> None:
    target = FakeReplyTarget()
    await target.text("hello")
    await target.image(b"bytes")
    await target.finish()
    assert target.sent == [("text", "hello"), ("image", b"bytes"), ("finish", None)]
