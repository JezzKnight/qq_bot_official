"""视频理解管线使用的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VideoPlatform = Literal["direct", "bilibili", "douyin", "other"]


@dataclass(frozen=True)
class VideoSource:
    """归一化后的视频来源。"""

    url: str
    platform: VideoPlatform
    video_id: str = ""


@dataclass(frozen=True)
class BilibiliMeta:
    """B 站视频元数据（用于字幕接口和提示词）。"""

    bvid: str
    aid: int
    cid: int
    title: str
    duration: int
    description: str = ""
    owner: str = ""


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 读出的基础媒体信息。"""

    duration: float
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class VideoFrame:
    """一张带时间戳的关键帧。"""

    timestamp: float
    jpeg: bytes


@dataclass(frozen=True)
class FetchedVideo:
    """下载/解析后的本地视频文件与元数据。"""

    path: str
    title: str = ""
    duration: float = 0.0
    webpage_url: str = ""
    uploader: str = ""
    description: str = ""


@dataclass(frozen=True)
class VideoInsight:
    """一次视频理解的最终结果。"""

    text: str
    title: str = ""
    duration: float = 0.0
    frame_count: int = 0
    subtitle_chars: int = 0
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
