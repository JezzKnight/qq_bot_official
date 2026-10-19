"""OneBot 事件 → Incoming。

注意：本模块不得 import 插件包（会触发 NoneBot 初始化），只依赖端口与通用媒体工具。
"""

from __future__ import annotations

from typing import Any

from qq_bot.media.video import video_urls_in_text
from qq_bot.platform.ports import Incoming, MediaRef, PlatformCaps, ReplyTarget

ONEBOT_CAPS = PlatformCaps(
    can_list_members=True,
    can_see_all_group_messages=True,
    can_send_image=True,
    can_active_message=True,
    max_reply_parts=1000,
)


def _sender_name(event: Any) -> str | None:
    sender = getattr(event, "sender", None)
    if sender is None:
        return None
    return getattr(sender, "card", None) or getattr(sender, "nickname", None) or None


def _quoted_prefix(event: Any) -> str:
    """把被引用消息转成一行可读摘要，保留原有行为。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return ""
    sender = getattr(reply, "sender", None)
    name = "未知"
    if sender is not None:
        name = (
            getattr(sender, "card", None)
            or getattr(sender, "nickname", None)
            or "未知"
        )
    parts: list[str] = []
    quoted = reply.message.extract_plain_text().strip()
    if quoted:
        parts.append(f'"{quoted}"')
    img_count = sum(1 for seg in reply.message if seg.type == "image")
    if img_count:
        parts.append(f"[{img_count} 张图片]")
    if not parts:
        return ""
    return f'[用户引用了 {name} 的消息：{"，".join(parts)}]\n'


def _segments(event: Any) -> list[Any]:
    segments = list(event.get_message())
    reply = getattr(event, "reply", None)
    if reply is not None:
        segments.extend(reply.message)
    return segments


def from_event(event: Any, *, reply: ReplyTarget, self_key: str = "") -> Incoming:
    """把 OneBot 事件归一化成 Incoming（图片只带 URL，由核心按需下载）。"""
    media: list[MediaRef] = []
    for seg in _segments(event):
        url = seg.data.get("url")
        if seg.type == "image" and url:
            media.append(MediaRef(kind="image", url=str(url)))
        elif seg.type == "video" and url:
            media.append(MediaRef(kind="video", url=str(url)))

    text = _quoted_prefix(event) + event.get_plaintext().strip()
    media.extend(
        MediaRef(kind="video", url=url) for url in video_urls_in_text(text)
    )

    sender = getattr(event, "sender", None)
    card = getattr(sender, "card", None) if sender is not None else None
    nickname = getattr(sender, "nickname", None) if sender is not None else None
    group_id = getattr(event, "group_id", None)
    return Incoming(
        platform="onebot",
        user_key=str(event.user_id),
        group_key=str(group_id) if group_id is not None else None,
        display_name=_sender_name(event),
        text=text,
        images=media,
        message_id=str(getattr(event, "message_id", "")),
        reply=reply,
        caps=ONEBOT_CAPS,
        self_key=str(self_key) or None,
        card=card or None,
        nickname=nickname or None,
    )
