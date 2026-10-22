"""从文本中识别视频链接，供各平台适配器复用。

直链按扩展名识别；B 站/抖音按平台 URL 规则识别（包括短链）。
"""

from __future__ import annotations

from qq_bot.video.source import (
    VIDEO_EXTENSIONS,
    is_bilibili_url,
    is_direct_video_url,
    is_douyin_url,
    is_video_url,
    video_urls_in_text,
)

__all__ = [
    "VIDEO_EXTENSIONS",
    "is_bilibili_url",
    "is_direct_video_url",
    "is_douyin_url",
    "is_video_url",
    "video_urls_in_text",
]
