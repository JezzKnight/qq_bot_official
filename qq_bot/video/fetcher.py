"""视频获取：直链用 httpx，平台页/短链用 yt-dlp。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .types import FetchedVideo, VideoSource

if TYPE_CHECKING:
    from .config import VideoConfig

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MEDIA_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".flv", ".ts"}


class VideoFetchError(RuntimeError):
    """下载或 yt-dlp 执行失败。"""


class VideoDependencyError(VideoFetchError):
    """缺少 ffmpeg/yt-dlp 等系统依赖。"""


def _suffix_for(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    suffix = Path(path).suffix.lower()
    return suffix if suffix in _MEDIA_EXTS else ".mp4"


async def download_direct(
    url: str,
    dest_dir: Path,
    *,
    config: VideoConfig,
    client: httpx.AsyncClient,
) -> FetchedVideo:
    """直链视频按原样下载，限制大小，避免把 QQ CDN 文件读进内存。"""
    dest = dest_dir / f"source{_suffix_for(url)}"
    total = 0
    try:
        async with client.stream("GET", url, headers={"User-Agent": _UA}) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            if content_type.startswith("text/html"):
                msg = "直链返回 HTML 页面而不是视频文件"
                raise VideoFetchError(msg)
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > config.max_bytes:
                        msg = f"视频超过大小上限 {config.max_bytes // 1024 // 1024}MB"
                        raise VideoFetchError(msg)
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        msg = f"视频下载失败: {type(exc).__name__}"
        raise VideoFetchError(msg) from exc
    finally:
        if total == 0 and dest.exists():
            dest.unlink(missing_ok=True)
    return FetchedVideo(path=str(dest))


def _ytdlp_format() -> str:
    # 低清优先：抽帧只需要可辨认画面，低清下载更快、更省流量。
    return "bv*[height<=480]+ba/b[height<=480]/b"


async def _run_ytdlp(url: str, dest_dir: Path, config: VideoConfig) -> str:
    binary = config.ytdlp
    if shutil.which(binary) is None:
        msg = f"未找到 yt-dlp（{binary}），请安装 yt-dlp 或配置 YTDLP_BIN"
        raise VideoDependencyError(msg)
    template = str(dest_dir / "source.%(ext)s")
    cmd = [
        binary,
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--write-info-json",
        "--merge-output-format",
        "mp4",
        "-f",
        _ytdlp_format(),
        "-o",
        template,
    ]
    if config.ytdlp_cookies:
        cmd.extend(["--cookies", config.ytdlp_cookies])
    if "bilibili.com" in url:
        cmd.extend(["--referer", "https://www.bilibili.com/"])
    cmd.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        msg = f"未找到 yt-dlp（{binary}）"
        raise VideoDependencyError(msg) from exc

    try:
        _stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=config.ytdlp_timeout
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        msg = f"yt-dlp 超时（{config.ytdlp_timeout:.0f}s）"
        raise VideoFetchError(msg) from exc
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-600:]
        msg = f"yt-dlp 下载失败: {detail or '未知错误'}"
        raise VideoFetchError(msg)
    return ""


def _find_media_file(dest_dir: Path) -> Path:
    candidates = [
        p
        for p in dest_dir.glob("source.*")
        if p.suffix.lower() in _MEDIA_EXTS and p.is_file()
    ]
    if not candidates:
        msg = "yt-dlp 执行成功但没有找到视频文件"
        raise VideoFetchError(msg)
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_info(dest_dir: Path) -> dict[str, Any]:
    info_path = dest_dir / "source.info.json"
    if not info_path.is_file():
        return {}
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def fetch_with_ytdlp(
    source: VideoSource,
    dest_dir: Path,
    *,
    config: VideoConfig,
) -> FetchedVideo:
    await _run_ytdlp(source.url, dest_dir, config)
    path = _find_media_file(dest_dir)
    info = _read_info(dest_dir)
    return FetchedVideo(
        path=str(path),
        title=str(info.get("title") or ""),
        duration=float(info.get("duration") or 0),
        webpage_url=str(info.get("webpage_url") or source.url),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        description=str(info.get("description") or ""),
    )


async def fetch(
    source: VideoSource,
    dest_dir: Path,
    *,
    config: VideoConfig,
) -> FetchedVideo:
    """按来源选择下载方式：直链不做转码，平台交给 yt-dlp。"""
    await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)
    if source.platform == "direct" and not source.url.lower().endswith(".m3u8"):
        timeout = httpx.Timeout(config.download_timeout)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            return await download_direct(
                source.url,
                dest_dir,
                config=config,
                client=client,
            )
    return await fetch_with_ytdlp(source, dest_dir, config=config)
