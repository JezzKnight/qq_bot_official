"""QQ 官方回复通道：首条被动回复，后续/表情走主动消息（避免每条都带 @）。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nonebot.adapters.qq import MessageSegment
from nonebot.adapters.qq.exception import ActionFailed

from .image_security import (
    ImageChecker,
    ImageCheckResult,
    get_image_security_checker,
)

if TYPE_CHECKING:
    from nonebot.adapters.qq.models.common import MessageKeyboard

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 25.0


def normalize_typing(text: str) -> str:
    """大肥鱼的打字习惯：聊天正文里的中文逗号改成空格。

    prompt 已经要求模型这样做；这里再兜一层，避免技术解释里偶尔漏掉。
    只替换中文逗号，代码里的英文逗号不动。
    """
    return text.replace("，", " ")


# QQ 开放平台：40034006 = 消息内容违规
CONTENT_VIOLATION_CODE = 40034006
CONTENT_VIOLATION_NOTICE = "发送图片失败，消息内容违规 QAQ"
IMAGE_BLOCKED_NOTICE = "这张图没通过安全审核，先不发了 QAQ"


class QQReply:
    """实现 ReplyTarget；官方每条被动消息最多回复 5 次，这里保守取 4 段。

    平台会把"带 msg_id 的被动回复"显示成对原消息的回复（客户端表现为 @ 对方）。
    因此只有首条走被动回复，后续分段与表情包走主动消息，避免每段都带 @。
    """

    max_parts = 4

    def __init__(
        self,
        bot: Any,
        event: Any,
        *,
        image_checker: ImageChecker | None = None,
    ) -> None:
        self._bot = bot
        self._event = event
        self._violation_notified = False
        self._image_checker = image_checker

    async def _send(self, seg: Any, *, passive: bool) -> None:
        logger.info("QQ send start passive=%s", passive)
        try:
            await asyncio.wait_for(self._send_once(seg, passive=passive), SEND_TIMEOUT)
        except asyncio.TimeoutError:
            logger.exception(
                "QQ send timeout after %ss passive=%s", SEND_TIMEOUT, passive
            )
            raise
        logger.info("QQ send done passive=%s", passive)

    async def _send_once(self, seg: Any, *, passive: bool) -> None:
        if passive:
            await self._bot.send(self._event, seg)
            return
        group_openid = getattr(self._event, "group_openid", None)
        if not group_openid:
            # 私聊没有"每段 @ 人"的问题，保持被动回复，避免占用主动消息额度
            await self._bot.send(self._event, seg)
            return
        try:
            await self._bot.send_to_group(group_openid=group_openid, message=seg)
        except ActionFailed as exc:
            if exc.code == CONTENT_VIOLATION_CODE:
                # 内容违规换被动回复也一样会被拒，直接交给 image() 发提示
                raise
            # 主动消息可能被用户关闭或超额：退回被动回复，至少保证送达
            logger.warning("QQ 主动消息发送失败，回退被动回复", exc_info=True)
            await self._bot.send(self._event, seg)
        except Exception:
            logger.warning("QQ 主动消息发送失败，回退被动回复", exc_info=True)
            await self._bot.send(self._event, seg)

    async def text(self, content: str) -> None:
        await self._send(MessageSegment.text(normalize_typing(content)), passive=False)

    async def passive(self, content: str) -> None:
        """指令回复：单条被动回复，显示为对原消息的回复。"""
        await self._send(MessageSegment.text(normalize_typing(content)), passive=True)

    async def mention(self, user_key: str, content: str) -> None:  # noqa: ARG002
        """首条回复：被动回复即可，平台会自动关联原消息。"""
        await self._send(MessageSegment.text(normalize_typing(content)), passive=True)

    async def markdown_panel(self, content: str, keyboard: MessageKeyboard) -> None:
        """发送 QQ 官方的 Markdown + 按钮面板（msg_type=2）。"""
        message = MessageSegment.markdown(content) + MessageSegment.keyboard(keyboard)
        await self._send(message, passive=True)

    async def image(self, data: bytes, name: str = "") -> bool:
        """发送图片；内容违规时只提示一次并返回 False，调用方可提前结束批量发图。"""
        if self._violation_notified:
            return False
        checker = self._image_checker or get_image_security_checker()
        try:
            result = await checker.check(data)
        except Exception:
            logger.exception("图片安全检测异常，按不合规拦截")
            result = ImageCheckResult(allowed=False, message="图片安全检测异常")
        if not result.allowed:
            logger.warning(
                "QQ 图片未通过安全审核 code=%s message=%s",
                result.code,
                result.message,
            )
            await self._notify_image_blocked()
            return False
        seg = MessageSegment.file_image(data, name or "image.png")
        try:
            await self._send(seg, passive=False)
            return True  # noqa: TRY300
        except ActionFailed as exc:
            if exc.code == CONTENT_VIOLATION_CODE or "违规" in (exc.message or ""):
                logger.warning(
                    "QQ 图片被判定内容违规 code=%s message=%s",
                    exc.code,
                    exc.message,
                )
                await self._notify_content_violation()
                return False
            raise

    async def _notify_image_blocked(self) -> None:
        """图片审核未通过时只提示一次，避免连续刷屏。"""
        if self._violation_notified:
            return
        self._violation_notified = True
        try:
            await self.text(IMAGE_BLOCKED_NOTICE)
        except Exception:
            logger.exception("发送图片安全拦截提示失败")

    async def _notify_content_violation(self) -> None:
        """违规提示只发一次；提示本身失败也不能再抛异常拖垮 handler。"""
        if self._violation_notified:
            return
        self._violation_notified = True
        try:
            await self.text(CONTENT_VIOLATION_NOTICE)
        except Exception:
            logger.exception("发送内容违规提示失败")

    async def finish(self) -> None:
        """官方适配器没有 finish 语义，回复发送完即可返回。"""
