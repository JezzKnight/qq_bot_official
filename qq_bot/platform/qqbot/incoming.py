"""QQ 官方事件 → Incoming。

官方给的是 openid（字符串），身份映射在 platform/identity 中完成；
群成员列表接口只返回 openid、没有昵称，因此显示名优先用事件里的 username，
缺失时退化为 openid 尾号。
"""

from __future__ import annotations

import os
from typing import Any

from qq_bot.platform.ports import Incoming, MediaRef, PlatformCaps, ReplyTarget

_FULL_GROUP = os.getenv("QQ_GROUP_FULL_MESSAGES", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

QQ_CAPS = PlatformCaps(
    can_list_members=False,
    can_see_all_group_messages=_FULL_GROUP,
    can_send_image=True,
    can_active_message=False,
    max_reply_parts=4,
    can_progress_messages=False,
)


def _display_name(author: Any, user_key: str) -> str:
    name = getattr(author, "username", None) if author is not None else None
    return name or f"用户{user_key[-6:]}"


def _quoted_prefix(event: Any) -> str:
    """把被引用的消息转成一行可读摘要（官方事件自带 reply.content/attachments）。"""
    quoted = getattr(event, "reply", None)
    if quoted is None:
        return ""
    author = getattr(quoted, "author", None)
    name = (getattr(author, "username", None) if author is not None else None) or "某人"
    content = (getattr(quoted, "content", "") or "").strip()
    attachments = getattr(quoted, "attachments", None) or []
    img_count = sum(
        1
        for item in attachments
        if str(getattr(item, "content_type", "")).startswith("image/")
    )
    parts: list[str] = []
    if content:
        parts.append(f'"{content}"')
    if img_count:
        parts.append(f"[{img_count} 张图片]")
    if not parts:
        return ""
    return f'[用户引用了 {name} 的消息：{"，".join(parts)}]\n'


def from_event(event: Any, *, reply: ReplyTarget) -> Incoming:
    """把 GroupAtMessageCreate / C2CMessageCreate 归一化成 Incoming。"""
    group_openid = getattr(event, "group_openid", None)
    author = getattr(event, "author", None)

    if group_openid:
        user_key = str(getattr(author, "member_openid", ""))
    else:
        user_key = str(getattr(author, "user_openid", ""))

    media: list[MediaRef] = []
    try:
        segments = list(event.get_message())
    except ValueError:
        segments = []
    for seg in segments:
        url = seg.data.get("url")
        if seg.type == "image" and url:
            media.append(MediaRef(kind="image", url=str(url)))
        elif seg.type == "video" and url:
            media.append(MediaRef(kind="video", url=str(url)))

    text = _quoted_prefix(event) + "".join(
        seg.data.get("text", "") for seg in segments if seg.type == "text"
    ).strip()

    return Incoming(
        platform="qq",
        user_key=user_key,
        group_key=str(group_openid) if group_openid else None,
        display_name=_display_name(author, user_key),
        text=text,
        images=media,
        message_id=str(getattr(event, "id", "")),
        reply=reply,
        caps=QQ_CAPS,
    )
