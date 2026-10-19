"""从文本中识别视频链接（按扩展名判定），供各平台适配器复用。"""

from __future__ import annotations

import re

URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；、]+", re.IGNORECASE)
VIDEO_EXTENSIONS = ("mp4", "webm", "mov", "mkv", "avi", "m4v", "3gp", "flv", "m3u8")


def video_urls_in_text(text: str) -> list[str]:
    """返回文本中所有指向视频文件的 URL。"""
    urls: list[str] = []
    for url in URL_PATTERN.findall(text or ""):
        path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
        if path.endswith(VIDEO_EXTENSIONS):
            urls.append(url)
    return urls
