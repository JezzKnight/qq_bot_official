"""视频理解编排：来源 → 下载 → 字幕 → scdet 关键帧 → 视觉模型。"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from qq_bot.platform.ports import MediaRef
from qq_bot.services import token_usage
from qq_bot.services.client_factory import (
    default_model_name,
    get_client_for_model,
    get_model_params,
)

from .analyzer import analyze_frames
from .bilibili import BilibiliError, fetch_meta, fetch_subtitles, resolve_b23
from .config import VideoConfig
from .fetcher import VideoFetchError, fetch
from .frames import VideoFrameError, make_keyframes
from .source import classify, is_safe_public_url, video_urls_in_text
from .types import VideoInsight, VideoSource

logger = logging.getLogger(__name__)


class VideoAgent:
    """一个视频一次分析；内部负责临时文件和资源清理。"""

    def __init__(
        self,
        *,
        config: VideoConfig | None = None,
        model: str = "",
        usage_recorder: Any = None,
    ) -> None:
        self.config = config or VideoConfig.from_env()
        self.model = model or self.config.vision_model or default_model_name()
        self.usage_recorder = usage_recorder

    def _source_from_input(self, raw: str | MediaRef) -> VideoSource:
        if isinstance(raw, MediaRef):
            if raw.url:
                url = raw.url
            elif raw.data:
                # 调用方应把 bytes 写入临时文件；这里只负责报错。
                msg = "暂不支持直接传入视频二进制，请先写入临时文件"
                raise ValueError(msg)
            else:
                msg = "视频引用缺少 URL"
                raise ValueError(msg)
        else:
            url = raw.strip()
        if not url:
            msg = "没有提供视频地址"
            raise ValueError(msg)
        if not is_safe_public_url(url):
            msg = "视频地址不是可访问的公网 http(s) 地址"
            raise ValueError(msg)
        # 文本里混着多个链接时只取第一个视频链接。
        urls = video_urls_in_text(url)
        if urls and urls[0] != url:
            url = urls[0]
        source = classify(url)
        # QQ 视频附件通常是 https://multimedia.nt.qq.com.cn/... 这种无扩展名 URL，
        # 但调用方已经明确 kind=video，不能交给 yt-dlp 去猜页面。
        if (
            isinstance(raw, MediaRef)
            and raw.kind == "video"
            and source.platform == "other"
        ):
            return VideoSource(url=url, platform="direct")
        return source

    async def _resolve_source(self, source: VideoSource) -> VideoSource:
        if source.platform != "bilibili" or "b23.tv" not in source.url:
            return source
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            resolved = await resolve_b23(client, source.url)
        if resolved != source.url:
            if not is_safe_public_url(resolved):
                msg = "B 站短链解析到了非公网地址"
                raise ValueError(msg)
            return classify(resolved)
        return source

    async def _bilibili_subtitles(
        self,
        source: VideoSource,
    ) -> tuple[str, str, list[str], str, str, float]:
        """返回字幕、语言、警告、标题、简介、时长。"""
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            try:
                if source.video_id.startswith("av"):
                    meta = await fetch_meta(client, aid=int(source.video_id[2:]))
                else:
                    meta = await fetch_meta(client, bvid=source.video_id)
            except (BilibiliError, httpx.HTTPError, ValueError) as exc:
                return "", "", [f"B 站元数据读取失败: {type(exc).__name__}"], "", "", 0
            text, lang, warnings = await fetch_subtitles(
                client,
                meta,
                cookie=self.config.bilibili_cookie,
                max_chars=self.config.max_subtitle_chars,
            )
            return (
                text,
                lang,
                warnings,
                meta.title,
                meta.description,
                float(meta.duration),
            )

    async def analyze(  # noqa: C901, PLR0915 —— 编排流程集中在这里
        self,
        raw: str | MediaRef,
        *,
        question: str = "",
    ) -> VideoInsight:
        started = time.monotonic()
        source = await self._resolve_source(self._source_from_input(raw))
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="qqbot-video-") as tmp:
            workdir = Path(tmp)
            title = ""
            description = ""
            transcript = ""
            subtitle_lang = ""
            meta_duration = 0.0

            subtitle_task = None
            if source.platform == "bilibili":
                subtitle_task = asyncio.create_task(self._bilibili_subtitles(source))
            try:
                fetched = await fetch(source, workdir, config=self.config)
            except (VideoFetchError, OSError) as exc:
                if subtitle_task is not None:
                    subtitle_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await subtitle_task
                msg = f"视频获取失败: {exc}"
                return VideoInsight(
                    text=msg,
                    warnings=[msg],
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            if subtitle_task is not None:
                try:
                    (
                        transcript,
                        subtitle_lang,
                        sub_warnings,
                        title,
                        description,
                        meta_duration,
                    ) = await subtitle_task
                    warnings.extend(sub_warnings)
                except asyncio.CancelledError:
                    warnings.append("字幕任务被取消")
                except Exception as exc:  # noqa: BLE001 —— 字幕只是增强，失败不影响抽帧
                    warnings.append(f"字幕读取异常: {type(exc).__name__}")

            title = title or fetched.title or "未命名视频"
            description = description or fetched.description
            try:
                info, frames = await make_keyframes(
                    fetched.path, workdir / "frames", self.config
                )
            except VideoFrameError as exc:
                msg = f"视频抽帧失败: {exc}"
                warnings.append(msg)
                return VideoInsight(
                    text=msg,
                    title=title,
                    duration=meta_duration or fetched.duration,
                    warnings=warnings,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            duration = info.duration or meta_duration or fetched.duration
            if self.config.max_duration and duration > self.config.max_duration:
                warnings.append(
                    f"视频时长 {duration:.0f}s 超过建议上限 "
                    f"{self.config.max_duration}s，"
                    "当前只抽取了有限关键帧"
                )
            if not transcript:
                warnings.append("没有可用字幕，结论主要依赖关键帧")

            client = await get_client_for_model(self.model)
            params = get_model_params(self.model)
            try:
                text = await analyze_frames(
                    client=client,
                    model=self.model,
                    source=source,
                    title=title,
                    duration=duration,
                    description=description,
                    transcript=transcript,
                    subtitle_lang=subtitle_lang,
                    frames=frames,
                    question=question,
                    usage_recorder=self.usage_recorder or token_usage.record,
                    max_tokens=min(3600, max(1600, params.max_tokens)),
                )
            except Exception as exc:
                logger.exception("视频理解模型调用失败")
                msg = f"视觉模型分析失败: {type(exc).__name__}"
                warnings.append(msg)
                text = msg

            body = [
                f"视频：{title}（{int(duration) // 60:02d}:{int(duration) % 60:02d}，"
                f"{len(frames)} 帧，字幕 {len(transcript)} 字）",
                text,
            ]
            if warnings:
                body.append("局限：" + "；".join(dict.fromkeys(warnings)))
            return VideoInsight(
                text="\n\n".join(body),
                title=title,
                duration=duration,
                frame_count=len(frames),
                subtitle_chars=len(transcript),
                warnings=list(dict.fromkeys(warnings)),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
