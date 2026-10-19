"""平台无关的接入端口：事件归一化后的 DTO 与回复通道协议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Platform = Literal["onebot", "qq"]


@dataclass(frozen=True)
class MediaRef:
    """消息里的媒体引用：图片/视频，二选一带 data（已下载）或 url（待下载/透传）。"""

    kind: Literal["image", "video"]
    data: bytes | None = None
    url: str | None = None


@dataclass(frozen=True)
class PlatformCaps:
    """平台能力标志：功能模块据此降级，而不是散落 if platform == ...。"""

    can_list_members: bool
    can_see_all_group_messages: bool
    can_send_image: bool
    can_active_message: bool
    max_reply_parts: int
    can_progress_messages: bool = True


class ReplyTarget(Protocol):
    """回复通道：核心只依赖这三个动作。"""

    max_parts: int

    async def text(self, content: str) -> None: ...

    async def mention(self, user_key: str, content: str) -> None: ...

    async def image(self, data: bytes, name: str = "") -> None: ...

    async def finish(self) -> None: ...


@dataclass
class Incoming:
    """一条已归一化的用户消息。"""

    platform: Platform
    user_key: str
    group_key: str | None
    display_name: str | None
    text: str
    images: list[MediaRef]
    message_id: str
    reply: ReplyTarget
    caps: PlatformCaps
    self_key: str | None = None
    card: str | None = None
    nickname: str | None = None

    @property
    def is_group(self) -> bool:
        return self.group_key is not None
