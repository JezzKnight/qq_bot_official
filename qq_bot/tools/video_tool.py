"""主模型可调用的视频理解工具。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from qq_bot.services import token_usage
from qq_bot.tools.context import current_videos
from qq_bot.tools.registry import register_tool
from qq_bot.video.pipeline import VideoAgent

if TYPE_CHECKING:
    from qq_bot.platform.ports import MediaRef

logger = logging.getLogger(__name__)

_MAX_QUESTION_CHARS = 500


def _pick_current_video() -> MediaRef | str | None:
    videos = current_videos.get() or []
    if not videos:
        return None
    return videos[0]


@register_tool(
    name="watch_video",
    description=(
        "看懂用户当前消息里的视频，或 B 站/抖音视频链接。"
        "会用 yt-dlp/httpx 获取视频、提取字幕和关键帧，再回答关于视频的问题。"
        "用户发了视频或视频链接时调用；视频文件必须用这个工具看，不能当图片。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "想从视频里知道什么，例如“讲了什么”“这个操作在第几分钟”",
            },
            "video": {
                "type": "string",
                "description": "可选：视频 URL；不填则分析当前消息里的第一个视频",
            },
        },
        "required": ["question"],
    },
)
async def watch_video(question: str, video: str = "") -> str:
    question = (question or "").strip()[:_MAX_QUESTION_CHARS]
    if not question:
        question = "请总结这个视频的内容"
    raw = (video or "").strip() or _pick_current_video()
    if raw is None:
        return "[视频解析失败] 当前消息里没有视频，也没有提供视频链接"
    try:
        agent = VideoAgent(usage_recorder=token_usage.record)
        insight = await agent.analyze(raw, question=question)
    except Exception as exc:
        logger.exception("watch_video 失败")
        return f"[视频解析失败] {type(exc).__name__}: {exc}"
    return insight.text
