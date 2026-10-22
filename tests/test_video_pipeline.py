"""视频工具的纯逻辑与 ffmpeg 集成测试。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from qq_bot.platform.ports import MediaRef
from qq_bot.video.bilibili import parse_subtitle_json
from qq_bot.video.config import VideoConfig
from qq_bot.video.fetcher import VideoFetchError, download_direct
from qq_bot.video.frames import (
    make_keyframes,
    parse_scdet_times,
    select_frame_times,
)
from qq_bot.video.pipeline import VideoAgent
from qq_bot.video.source import (
    classify,
    is_bilibili_url,
    is_douyin_url,
    is_video_url,
    urls_from_payload,
    video_urls_in_text,
)

FFMPEG = shutil.which("ffmpeg")


def test_video_url_recognition_and_normalization() -> None:
    assert is_bilibili_url("https://www.bilibili.com/video/BV1xx411c7mD")
    assert is_bilibili_url("https://b23.tv/abc123")
    assert not is_bilibili_url("https://example.com/video/BV1xx411c7mD")
    assert is_douyin_url("https://v.douyin.com/abc123/")
    assert is_video_url("https://cdn.example.com/a.mp4?x=1")
    assert not is_video_url("https://example.com/article")

    text = "看这个 https://www.bilibili.com/video/BV1xx411c7mD，还有 https://example.com/a.mp4"
    assert video_urls_in_text(text) == [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://example.com/a.mp4",
    ]

    source = classify("https://www.bilibili.com/video/BV1xx411c7mD")
    assert source.platform == "bilibili"
    assert source.video_id == "BV1xx411c7mD"

    av_source = classify("https://www.bilibili.com/video/av170001")
    assert av_source.platform == "bilibili"
    assert av_source.video_id == "av170001"


def test_urls_from_forward_card_payload() -> None:
    payload = {
        "app": "com.tencent.miniapp",
        "prompt": "[分享] 视频",
        "meta": {"detail_1": {"qqdocurl": "https://b23.tv/abc123"}},
    }
    assert urls_from_payload(payload) == ["https://b23.tv/abc123"]


def test_direct_media_ref_without_extension_is_treated_as_direct() -> None:
    agent = VideoAgent(config=VideoConfig(), model="test")
    source = agent._source_from_input(
        MediaRef(kind="video", url="https://multimedia.nt.qq.com.cn/download?id=1")
    )
    assert source.platform == "direct"


def test_parse_scdet_times_from_ffmpeg_metadata() -> None:
    output = """
    [Parsed_metadata_1] frame:0 pts:0 pts_time:0
    [Parsed_metadata_1] lavfi.scd.time=1.23
    [Parsed_metadata_1] frame:2 pts:100 pts_time:2.50
    [Parsed_metadata_1] frame:3 pts:200 pts_time:2.50
    """
    assert parse_scdet_times(output) == [0.0, 1.23, 2.5]


def test_select_frame_times_keeps_head_and_limit() -> None:
    times = select_frame_times([1.0, 2.0, 3.0, 4.0], 10.0, max_frames=4)
    assert times[0] == 0.0
    assert len(times) == 4
    assert times[-1] == 9.9


def test_parse_bilibili_subtitle_json_with_timestamps() -> None:
    payload = {
        "body": [
            {"from": 0.0, "to": 1.0, "content": "第一句"},
            {"from": 61.5, "to": 62.0, "content": "第二句"},
            {"from": 62.0, "to": 63.0, "content": ""},
        ]
    }
    text = parse_subtitle_json(payload, max_chars=100)
    assert text == "[00:00] 第一句\n[01:01] 第二句"


def test_parse_bilibili_subtitle_respects_char_limit() -> None:
    payload = {"body": [{"from": 0, "content": "a" * 20}]}
    assert parse_subtitle_json(payload, max_chars=10) == ""


async def test_download_direct_rejects_html(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html>not a video</html>",
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(VideoFetchError, match="HTML"):
            await download_direct(
                "https://example.com/video",
                tmp_path,
                config=VideoConfig(),
                client=client,
            )


@pytest.mark.skipif(FFMPEG is None, reason="需要 ffmpeg")
async def test_make_keyframes_with_generated_video(tmp_path: Path) -> None:
    video = tmp_path / "sample.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    info, frames = await make_keyframes(
        str(video),
        tmp_path / "frames",
        VideoConfig(max_frames=4, frame_width=320),
    )
    assert info.duration > 1.0
    assert 1 <= len(frames) <= 4
    assert frames[0].jpeg.startswith(b"\xff\xd8")


async def test_bilibili_subtitles_sends_cookie_and_reads_cc() -> None:
    from qq_bot.video.bilibili import BilibiliMeta, fetch_subtitles

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/x/player/v2"):
            seen["cookie"] = request.headers.get("cookie", "")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "subtitle": {
                            "subtitles": [
                                {
                                    "lan": "zh-CN",
                                    "lan_doc": "中文（自动）",
                                    "subtitle_url": "//example.com/sub.json",
                                }
                            ]
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={"body": [{"from": 1.5, "content": "你好"}]},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        text, lang, warnings = await fetch_subtitles(
            client,
            BilibiliMeta(
                bvid="BV1xx411c7mD",
                aid=1,
                cid=2,
                title="t",
                duration=10,
            ),
            cookie="SESSDATA=abc",
            max_chars=100,
        )
    assert text == "[00:01] 你好"
    assert lang == "中文（自动）"
    assert warnings == []
    assert seen["cookie"] == "SESSDATA=abc"


async def test_bilibili_subtitles_explains_login_requirement() -> None:
    from qq_bot.video.bilibili import BilibiliMeta, fetch_subtitles

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"need_login_subtitle": True, "subtitle": {"subtitles": []}},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        text, lang, warnings = await fetch_subtitles(
            client,
            BilibiliMeta(bvid="BV1xx411c7mD", aid=1, cid=2, title="t", duration=10),
        )
    assert text == ""
    assert lang == ""
    assert warnings == ["B 站匿名无法获取 AI 字幕，需配置 BILIBILI_COOKIE"]
