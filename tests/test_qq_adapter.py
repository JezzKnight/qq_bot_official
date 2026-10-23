"""QQ 官方适配器：事件归一化与回复通道。"""

from __future__ import annotations

from dataclasses import dataclass, field

from qq_bot.platform.qqbot.incoming import (
    QQ_CAPS,
    from_event,
    is_group_trigger,
    mentions_me,
)
from qq_bot.platform.qqbot.reply import QQReply


@dataclass
class FakeSeg:
    type: str
    data: dict = field(default_factory=dict)


class FakeMessage(list):
    pass


class FakeEvent:
    def __init__(  # noqa: PLR0913
        self,
        *,
        group_openid: str | None = None,
        member_openid: str = "openid-user-123456",
        user_openid: str = "openid-friend-654321",
        username: str | None = None,
        message: list[FakeSeg] | None = None,
        reply: object | None = None,
        msg_id: str = "m1",
    ) -> None:
        self.group_openid = group_openid
        self.author = type(
            "Author",
            (),
            {
                "member_openid": member_openid,
                "user_openid": user_openid,
                "username": username,
            },
        )()
        self.id = msg_id
        self.reply = reply
        self._message = FakeMessage(message or [])

    def get_message(self) -> FakeMessage:
        return self._message


class FakeReply:
    max_parts = 4

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def text(self, content: str) -> None:
        self.sent.append(("text", content))

    async def passive(self, content: str) -> None:
        self.sent.append(("passive", content))

    async def mention(self, user_key: str, _content: str) -> None:
        self.sent.append(("mention", user_key))

    async def image(self, _data: bytes, name: str = "") -> None:
        self.sent.append(("image", name))

    async def finish(self) -> None:
        return


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.active: list[object] = []

    async def send(self, _event, message) -> None:
        self.sent.append(message)

    async def send_to_group(self, group_openid, message) -> None:  # noqa: ARG002
        self.active.append(message)

    async def send_to_c2c(self, openid, message) -> None:  # noqa: ARG002
        self.active.append(message)


def test_group_event_is_normalized() -> None:
    event = FakeEvent(
        group_openid="group-openid-1",
        username="群友甲",
        message=[FakeSeg("text", {"text": " 你好 "})],
    )
    incoming = from_event(event, reply=FakeReply())
    assert incoming.user_key == "openid-user-123456"
    assert incoming.group_key == "group-openid-1"
    assert incoming.is_group is True
    assert incoming.display_name == "群友甲"
    assert incoming.text == "你好"
    assert incoming.caps == QQ_CAPS
    assert incoming.caps.max_reply_parts == 4


def test_display_name_falls_back_to_openid_tail() -> None:
    event = FakeEvent(group_openid="g", username=None)
    incoming = from_event(event, reply=FakeReply())
    assert incoming.display_name == "用户123456"


def test_c2c_event_has_no_group() -> None:
    event = FakeEvent(group_openid=None, username="鱼片")
    incoming = from_event(event, reply=FakeReply())
    assert incoming.group_key is None
    assert incoming.user_key == "openid-friend-654321"


def test_quoted_message_becomes_prefix() -> None:
    event = FakeEvent(
        group_openid="g",
        message=[FakeSeg("text", {"text": "这是啥"})],
    )
    event.reply = type(
        "Quoted",
        (),
        {
            "content": "原话",
            "attachments": [type("Att", (), {"content_type": "image/png"})()],
            "author": type("A", (), {"username": "小红"})(),
        },
    )()
    incoming = from_event(event, reply=FakeReply())
    assert incoming.text.startswith('[用户引用了 小红 的消息："原话"，[1 张图片]]')
    assert incoming.text.endswith("这是啥")


def test_image_segment_becomes_media_ref() -> None:
    event = FakeEvent(
        group_openid="g",
        message=[
            FakeSeg("image", {"url": "https://x/a.png"}),
            FakeSeg("text", {"text": "看图"}),
        ],
    )
    incoming = from_event(event, reply=FakeReply())
    assert [(m.kind, m.url) for m in incoming.images] == [("image", "https://x/a.png")]


async def test_first_segment_is_passive_rest_are_active() -> None:
    bot = FakeBot()
    reply = QQReply(bot, FakeEvent(group_openid="g"))
    await reply.mention("openid-user-123456", "第一段")
    await reply.text("第二段")
    await reply.image(b"bytes", "s.png")
    await reply.finish()
    # 首条被动回复（平台自动关联原消息），后续与表情走主动消息，不带 @
    assert len(bot.sent) == 1
    assert len(bot.active) == 2


class FakeMention:
    def __init__(self, *, is_you: bool) -> None:
        self.is_you = is_you


class FakeTypedEvent:
    """只用于触发判定：用 __type__ 模拟适配器事件类型。"""

    def __init__(
        self, event_type: str, mentions: list[FakeMention] | None = None
    ) -> None:
        self.__type__ = event_type
        self.mentions = mentions
        self._message: list[FakeSeg] = []

    def get_message(self) -> list[FakeSeg]:
        return self._message


def test_group_at_event_is_trigger() -> None:
    assert is_group_trigger(FakeTypedEvent("GROUP_AT_MESSAGE_CREATE")) is True


def test_full_message_with_is_you_is_trigger() -> None:
    event = FakeTypedEvent("GROUP_MESSAGE_CREATE", mentions=[FakeMention(is_you=True)])
    assert mentions_me(event) is True
    assert is_group_trigger(event) is True


def test_full_message_without_mention_is_not_trigger() -> None:
    event = FakeTypedEvent("GROUP_MESSAGE_CREATE", mentions=[FakeMention(is_you=False)])
    assert mentions_me(event) is False
    assert is_group_trigger(event) is False


def test_mention_segment_with_is_bot_is_trigger() -> None:
    event = FakeTypedEvent("GROUP_MESSAGE_CREATE")
    event._message = [
        FakeSeg("mention_user", {"user_id": "bot-openid", "is_bot": True})
    ]
    assert mentions_me(event) is True


class FakeAttachment:
    def __init__(self, content_type: str, url: str | None) -> None:
        self.content_type = content_type
        self.url = url


class FakeQuoted:
    def __init__(self, content: str, attachments: list[FakeAttachment]) -> None:
        self.content = content
        self.attachments = attachments
        self.author = type("Author", (), {"username": "阿强"})()


def test_quoted_image_is_added_to_media() -> None:
    event = FakeEvent(
        group_openid="group-openid-1",
        message=[FakeSeg("text", {"text": "看这个"})],
        reply=FakeQuoted(
            "原消息",
            [FakeAttachment("image/jpeg", "https://example.com/quoted.jpg")],
        ),
    )
    incoming = from_event(event, reply=FakeReply())
    assert incoming.images[-1].kind == "image"
    assert incoming.images[-1].url == "https://example.com/quoted.jpg"
    assert "[1 张图片]" in incoming.text
    assert "原消息" in incoming.text


def test_quoted_image_without_url_is_ignored() -> None:
    event = FakeEvent(
        group_openid="group-openid-1",
        message=[FakeSeg("text", {"text": "看这个"})],
        reply=FakeQuoted("原消息", [FakeAttachment("image/jpeg", None)]),
    )
    incoming = from_event(event, reply=FakeReply())
    assert incoming.images == []
    assert "[1 张图片]" in incoming.text


def test_video_segment_and_text_link_become_video_refs() -> None:
    event = FakeEvent(
        group_openid="g",
        message=[
            FakeSeg("video", {"url": "https://multimedia.nt.qq.com.cn/video?id=1"}),
            FakeSeg(
                "text", {"text": "再看 https://www.bilibili.com/video/BV1xx411c7mD"}
            ),
        ],
    )
    incoming = from_event(event, reply=FakeReply())
    assert [(m.kind, m.url) for m in incoming.images] == [
        ("video", "https://multimedia.nt.qq.com.cn/video?id=1"),
        ("video", "https://www.bilibili.com/video/BV1xx411c7mD"),
    ]


def test_forward_card_ark_segment_becomes_video_ref() -> None:
    event = FakeEvent(
        group_openid="g",
        message=[
            FakeSeg(
                "ark",
                {
                    "prompt": "[分享] 视频",
                    "meta": {"detail_1": {"qqdocurl": "https://b23.tv/abc123"}},
                },
            )
        ],
    )
    incoming = from_event(event, reply=FakeReply())
    assert [(m.kind, m.url) for m in incoming.images] == [
        ("video", "https://b23.tv/abc123")
    ]
