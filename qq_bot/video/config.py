"""视频理解配置：全部来自环境变量，便于在 WSL/生产环境调整。"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_binary(name: str) -> str:
    """优先 PATH；uv 装进 .venv/bin 的 yt-dlp 不在 PATH 时也能找到。"""
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(sys.executable).with_name(name)
    return str(candidate) if candidate.exists() else name


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class VideoConfig:
    """视频下载、抽帧与分析参数。"""

    max_duration: int = 300
    max_frames: int = 12
    max_bytes: int = 80 * 1024 * 1024
    frame_width: int = 768
    scene_threshold: float = 10.0
    max_subtitle_chars: int = 12_000
    vision_model: str = ""
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    ytdlp: str = "yt-dlp"
    bilibili_cookie: str = ""
    ytdlp_cookies: str = ""
    download_timeout: float = 90.0
    ytdlp_timeout: float = 120.0

    @classmethod
    def from_env(cls) -> VideoConfig:
        return cls(
            max_duration=_int_env("VIDEO_MAX_DURATION", 300),
            max_frames=max(3, _int_env("VIDEO_MAX_FRAMES", 12)),
            max_bytes=max(1, _int_env("VIDEO_MAX_MB", 80)) * 1024 * 1024,
            frame_width=max(320, _int_env("VIDEO_FRAME_WIDTH", 768)),
            scene_threshold=_float_env("VIDEO_SCENE_THRESHOLD", 10.0),
            max_subtitle_chars=max(1000, _int_env("VIDEO_MAX_SUBTITLE_CHARS", 12_000)),
            vision_model=os.getenv("VIDEO_VISION_MODEL", "").strip(),
            ffmpeg=os.getenv("FFMPEG_BIN", "").strip() or _default_binary("ffmpeg"),
            ffprobe=os.getenv("FFPROBE_BIN", "").strip() or _default_binary("ffprobe"),
            ytdlp=os.getenv("YTDLP_BIN", "").strip() or _default_binary("yt-dlp"),
            bilibili_cookie=os.getenv("BILIBILI_COOKIE", "").strip(),
            ytdlp_cookies=os.getenv("VIDEO_YTDLP_COOKIES", "").strip(),
            download_timeout=_float_env("VIDEO_DOWNLOAD_TIMEOUT", 90.0),
            ytdlp_timeout=_float_env("VIDEO_YTDLP_TIMEOUT", 120.0),
        )
