"""ffmpeg/ffprobe 封装：媒体探测、scdet 场景检测、关键帧抽取。

抽帧策略：
1. 先用 ffmpeg 的 `scdet` 找镜头切换点；
2. 在场景点里均匀取样，始终保留开头；
3. 场景点不足时用等间隔时间点补足；
4. 每个时间点单独抽一张 JPEG，交给视觉模型。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from .types import MediaInfo, VideoFrame

if TYPE_CHECKING:
    from pathlib import Path

    from .config import VideoConfig

logger = logging.getLogger(__name__)

_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_SCD_TIME_RE = re.compile(r"lavfi\.scd\.time=([0-9]+(?:\.[0-9]+)?)")
_JPEG_QUALITY = "3"
_MAX_SUBPROCESS_OUTPUT = 4000


class VideoFrameError(RuntimeError):
    """ffmpeg/ffprobe 执行失败。"""


class VideoDependencyError(VideoFrameError):
    """缺少 ffmpeg/ffprobe。"""


async def _run(cmd: list[str], *, timeout: float = 60.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        msg = f"未找到 {cmd[0]}，请安装 ffmpeg 或配置 FFMPEG_BIN/FFPROBE_BIN"
        raise VideoDependencyError(msg) from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        msg = f"{cmd[0]} 超时（{timeout:.0f}s）"
        raise VideoFrameError(msg) from exc
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT],
        stderr.decode("utf-8", errors="replace")[:_MAX_SUBPROCESS_OUTPUT],
    )


async def probe(path: str, config: VideoConfig) -> MediaInfo:
    cmd = [
        config.ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=width,height",
        "-of",
        "json",
        path,
    ]
    code, stdout, stderr = await _run(cmd)
    if code != 0:
        msg = f"ffprobe 读取视频失败: {stderr.strip()[-400:]}"
        raise VideoFrameError(msg)
    import json

    try:
        data = json.loads(stdout)
    except ValueError as exc:
        msg = "ffprobe 返回了非法 JSON"
        raise VideoFrameError(msg) from exc
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    width = 0
    height = 0
    for stream in data.get("streams") or []:
        if stream.get("width") and stream.get("height"):
            width = int(stream["width"])
            height = int(stream["height"])
            break
    return MediaInfo(duration=duration, width=width, height=height)


def parse_scdet_times(output: str) -> list[float]:
    """解析 `scdet=...,metadata=print` 的 pts_time；兼容 lavfi.scd.time。"""
    times: list[float] = []
    for line in output.splitlines():
        match = _PTS_RE.search(line) or _SCD_TIME_RE.search(line)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if value >= 0:
            times.append(value)
    return sorted(set(times))


async def detect_scene_times(path: str, config: VideoConfig) -> list[float]:
    cmd = [
        config.ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        path,
        "-vf",
        f"scdet=threshold={config.scene_threshold},metadata=print:file=-",
        "-an",
        "-f",
        "null",
        "-",
    ]
    code, stdout, stderr = await _run(cmd, timeout=90.0)
    if code != 0:
        logger.info("scdet 失败，回退等间隔抽帧: %s", stderr.strip()[-200:])
        return []
    return parse_scdet_times(f"{stdout}\n{stderr}")


def select_frame_times(
    scene_times: list[float],
    duration: float,
    *,
    max_frames: int,
) -> list[float]:
    """场景点优先，均匀取样；保证不超过 max_frames 且包含开头。"""
    limit = max(1, max_frames)
    duration = max(0.0, duration)
    candidates = [0.0]
    candidates.extend(t for t in scene_times if 0.0 < t < max(duration, 1.0))
    if duration > 1.0:
        candidates.append(max(0.0, duration - 0.1))
    unique = sorted({round(t, 2) for t in candidates})
    if len(unique) <= limit:
        return unique
    # 均匀选 limit 个，同时保留第一个镜头。
    step = (len(unique) - 1) / (limit - 1) if limit > 1 else 0
    picked = {unique[round(i * step)] for i in range(limit)}
    return sorted(picked)


def _frame_path(out_dir: Path, index: int, timestamp: float) -> Path:
    minute, second = divmod(int(timestamp), 60)
    return out_dir / f"frame_{index:02d}_{minute:02d}m{second:02d}s.jpg"


async def extract_frames(
    path: str,
    times: list[float],
    out_dir: Path,
    config: VideoConfig,
) -> list[VideoFrame]:
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    frames: list[VideoFrame] = []
    for index, timestamp in enumerate(times):
        target = _frame_path(out_dir, index, timestamp)
        cmd = [
            config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({config.frame_width},iw)':-2",
            "-q:v",
            _JPEG_QUALITY,
            "-y",
            str(target),
        ]
        code, _stdout, stderr = await _run(cmd, timeout=30.0)
        if code != 0 or not target.is_file():
            logger.info("抽帧失败 ts=%.2f: %s", timestamp, stderr.strip()[-200:])
            continue
        frames.append(VideoFrame(timestamp=timestamp, jpeg=target.read_bytes()))
        target.unlink(missing_ok=True)
    if not frames:
        msg = "视频关键帧抽取失败（可能是编码不支持或视频为空）"
        raise VideoFrameError(msg)
    return frames


async def make_keyframes(
    path: str,
    out_dir: Path,
    config: VideoConfig,
) -> tuple[MediaInfo, list[VideoFrame]]:
    info = await probe(path, config)
    scene_times = await detect_scene_times(path, config)
    times = select_frame_times(
        scene_times,
        info.duration,
        max_frames=config.max_frames,
    )
    frames = await extract_frames(path, times, out_dir, config)
    return info, frames
