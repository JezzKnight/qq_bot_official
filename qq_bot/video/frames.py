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
_SCD_TIME_RE = re.compile(r"lavfi\.scd\.time[=:]([0-9]+(?:\.[0-9]+)?)")
_SCD_SCORE_RE = re.compile(r"lavfi\.scd\.score[=:]([0-9]+(?:\.[0-9]+)?)")
_JPEG_QUALITY = "3"
_MAX_SUBPROCESS_OUTPUT = 4000
_MIN_VIDEO_DURATION = 0.01


class VideoFrameError(RuntimeError):
    """ffmpeg/ffprobe 执行失败。"""


class VideoDependencyError(VideoFrameError):
    """缺少 ffmpeg/ffprobe。"""


async def _run(
    cmd: list[str],
    *,
    timeout: float = 60.0,
    max_output: int | None = _MAX_SUBPROCESS_OUTPUT,
) -> tuple[int, str, str]:
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
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if max_output is not None:
        stdout_text = stdout_text[:max_output]
        stderr_text = stderr_text[:max_output]
    return proc.returncode or 0, stdout_text, stderr_text


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


def parse_scdet_times(  # noqa: C901, PLR0912 —— 兼容三种 ffmpeg 输出格式
    output: str, *, threshold: float = 10.0
) -> list[float]:
    """解析 scdet 输出。

    ffmpeg 的 `metadata=print` 默认会为每一帧打印 pts_time，同时打印
    `lavfi.scd.score`。如果只抓 pts_time，会把所有帧都当成“场景”，既慢又会
    在输出被截断时只看到片头。这里优先使用 `lavfi.scd.time`，否则只接受
    score >= threshold 的帧；两者都没有时再退回旧的 pts_time 解析。
    """
    explicit: list[float] = []
    scored: list[float] = []
    saw_score = False
    current: float | None = None
    for line in output.splitlines():
        pts_match = _PTS_RE.search(line)
        if pts_match:
            try:
                current = float(pts_match.group(1))
            except ValueError:
                current = None
        time_match = _SCD_TIME_RE.search(line)
        if time_match:
            try:
                explicit.append(float(time_match.group(1)))
            except ValueError:
                continue
            continue
        score_match = _SCD_SCORE_RE.search(line)
        if score_match and current is not None:
            saw_score = True
            try:
                score = float(score_match.group(1))
            except ValueError:
                continue
            if score >= threshold:
                scored.append(current)

    if explicit:
        return sorted({round(t, 3) for t in explicit if t >= 0})
    if scored:
        return sorted({round(t, 3) for t in scored if t >= 0})
    if saw_score:
        # 有 score 但全部低于阈值，说明没有镜头切换；交给均匀分桶，
        # 不能把每一帧都当场景，否则长视频会生成巨大的列表。
        return []

    # 兼容只打印 pts_time 的旧 ffmpeg 输出/测试桩。
    fallback: list[float] = []
    for line in output.splitlines():
        match = _PTS_RE.search(line)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if value >= 0:
                fallback.append(value)
    return sorted({round(t, 3) for t in fallback})


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
    # scdet 会逐帧打印 metadata，5 分钟视频约 1MB；不能用默认的 4KB 截断，
    # 否则只能解析到片头几秒，最终抽帧全是黑屏/转场。
    code, stdout, stderr = await _run(cmd, timeout=90.0, max_output=None)
    if code != 0:
        logger.info("scdet 失败，回退等间隔抽帧: %s", stderr.strip()[-200:])
        return []
    times = parse_scdet_times(f"{stdout}\n{stderr}", threshold=config.scene_threshold)
    logger.info(
        "scdet 完成: scenes=%d first=%s last=%s",
        len(times),
        times[:3],
        times[-3:],
    )
    return times


def select_frame_times(
    scene_times: list[float],
    duration: float,
    *,
    max_frames: int,
) -> list[float]:
    """按时间分桶取样；桶内有场景点时优先场景点，否则取桶中心。

    旧实现只把“0 + 场景点 + 片尾”作为候选。一旦 scdet 没解析出场景点
    （或输出被截断），整段视频就只剩开头和结尾两帧，长视频会全是黑屏/
    转场。分桶能保证无论场景检测是否成功，都覆盖整个视频。
    """
    limit = max(1, max_frames)
    duration = max(0.0, duration)
    if duration <= _MIN_VIDEO_DURATION:
        return [0.0]

    # 10 秒 1 帧，至少 4 帧，最多 max_frames。
    target = min(limit, max(4, int(duration // 10) + 1))
    span = max(duration - 0.2, 0.1)
    picked: list[float] = []
    for index in range(target):
        low = span * index / target
        high = span * (index + 1) / target
        bucket = sorted(t for t in scene_times if low <= t < high)
        # 桶内首帧通常是转场，取靠中间的稳定画面。
        value = bucket[len(bucket) // 2] if bucket else (low + high) / 2
        picked.append(round(max(0.0, min(value, duration - 0.05)), 2))

    unique = sorted(set(picked))
    if len(unique) >= target:
        return unique[:target]
    # 极端短片/时间点重合时补等间隔点，避免少于目标帧数。
    for index in range(target):
        value = round(span * (index + 0.5) / target, 2)
        if value not in unique:
            unique.append(value)
    return sorted(unique)[:target]


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
    logger.info(
        "视频抽帧完成: duration=%.1fs size=%dx%d scenes=%d selected=%s frames=%d",
        info.duration,
        info.width,
        info.height,
        len(scene_times),
        times,
        len(frames),
    )
    return info, frames
