"""视频链接识别与来源归一化。

支持的来源：
- 直链视频：mp4/webm/mov/mkv/...；
- B 站：bilibili.com/video/BV...、av...、b23.tv 短链；
- 抖音：douyin.com/video/...、v.douyin.com 短链。

这里只做“识别”，不负责网络请求，便于单元测试。
"""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit

from .types import VideoSource

URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；、）)】]+", re.IGNORECASE)
VIDEO_EXTENSIONS = ("mp4", "webm", "mov", "mkv", "avi", "m4v", "3gp", "flv", "m3u8")
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"/video/av(\d+)", re.IGNORECASE)
_B23_RE = re.compile(r"(?:https?://)?b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
_DOUYIN_RE = re.compile(
    r"(?:https?://)?(?:v\.douyin\.com/[0-9A-Za-z]+|(?:www\.)?douyin\.com/video/\d+)",
    re.IGNORECASE,
)
_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}
_DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "v.douyin.com"}


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_private_host(host: str) -> bool:
    """阻止把视频工具变成访问内网的服务。"""
    if (
        not host
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith(".local")
    ):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def is_safe_public_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    return not _is_private_host(parts.hostname or "")


def _strip_trailing(url: str) -> str:
    return url.rstrip(".,;:!?，。；：！？、）)】》」』")


def is_direct_video_url(url: str) -> bool:
    path = urlsplit(url).path.lower().rstrip("/")
    return path.endswith(tuple(f".{ext}" for ext in VIDEO_EXTENSIONS))


def is_bilibili_url(url: str) -> bool:
    host = _host_of(url)
    if host not in _BILIBILI_HOSTS:
        return False
    return bool(_BV_RE.search(url) or _AV_RE.search(url) or host == "b23.tv")


def is_douyin_url(url: str) -> bool:
    host = _host_of(url)
    if host not in _DOUYIN_HOSTS:
        return False
    return bool(_DOUYIN_RE.search(url))


def is_video_url(url: str) -> bool:
    return is_direct_video_url(url) or is_bilibili_url(url) or is_douyin_url(url)


def _bvid(url: str) -> str:
    m = _BV_RE.search(url)
    return m.group(1) if m else ""


def _avid(url: str) -> str:
    m = _AV_RE.search(url)
    return f"av{m.group(1)}" if m else ""


def classify(url: str) -> VideoSource:
    if is_bilibili_url(url):
        return VideoSource(
            url=url,
            platform="bilibili",
            video_id=_bvid(url) or _avid(url),
        )
    if is_douyin_url(url):
        return VideoSource(url=url, platform="douyin")
    if is_direct_video_url(url):
        return VideoSource(url=url, platform="direct")
    return VideoSource(url=url, platform="other")


def video_urls_in_text(text: str) -> list[str]:
    """返回文本中所有视频 URL（平台页/短链/直链），保持出现顺序并去重。"""
    found: list[str] = []
    seen: set[str] = set()
    for raw in URL_PATTERN.findall(text or ""):
        url = _strip_trailing(raw)
        if not is_safe_public_url(url) or not is_video_url(url):
            continue
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


def urls_from_payload(payload: object) -> list[str]:
    """从 QQ ark/转发卡片等任意结构里挖出视频 URL。"""
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return []
    return video_urls_in_text(text)
