"""视频笔记缓存的单元测试：命中时不触发下载/抽帧/视觉调用。"""

from __future__ import annotations

import time
from typing import Any

import pytest

from qq_bot.video import pipeline
from qq_bot.video.config import VideoConfig
from qq_bot.video.pipeline import VideoAgent
from qq_bot.video.source import video_cache_key
from qq_bot.video.types import VideoInsight, VideoSource


class FakeNoteStore:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.saved: dict[str, Any] | None = None
        self.get_calls = 0

    async def get_video_note(self, _video_key: str) -> dict[str, Any] | None:
        self.get_calls += 1
        return self.row

    async def save_video_note(self, **kwargs: Any) -> None:
        self.saved = kwargs


def _agent(store: FakeNoteStore) -> VideoAgent:
    return VideoAgent(config=VideoConfig(), model="fake-model", note_store=store)


def test_video_cache_key_keeps_bilibili_page() -> None:
    source = VideoSource(
        url="https://www.bilibili.com/video/BV1xx411c7mD?p=2",
        platform="bilibili",
        video_id="BV1xx411c7mD",
    )
    assert video_cache_key(source) == "bilibili:BV1xx411c7mD:p2"


def test_video_cache_key_douyin_uses_numeric_id() -> None:
    source = VideoSource(
        url="https://www.douyin.com/video/7412345678901234567",
        platform="douyin",
    )
    assert video_cache_key(source) == "douyin:7412345678901234567"


@pytest.mark.asyncio
async def test_cached_insight_returns_note_without_analysis() -> None:
    store = FakeNoteStore(
        {
            "note": "这是之前保存的视频笔记",
            "title": "测试视频",
            "duration": 12.5,
            "frame_count": 3,
            "subtitle_chars": 8,
            "hit_count": 2,
        }
    )
    insight = await _agent(store)._cached_insight("bilibili:BV1:p1", time.monotonic())
    assert insight is not None
    assert insight.text == "这是之前保存的视频笔记"
    assert insight.title == "测试视频"
    assert insight.frame_count == 3
    assert store.get_calls == 1


@pytest.mark.asyncio
async def test_analyze_uses_cache_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeNoteStore({"note": "缓存内容", "title": "缓存标题"})
    called = False

    async def fail_fetch(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True
        raise AssertionError("命中缓存后不应该下载视频")

    monkeypatch.setattr(pipeline, "fetch", fail_fetch)
    insight = await _agent(store).analyze(
        "https://www.bilibili.com/video/BV1xx411c7mD", question="讲了什么"
    )
    assert insight.text == "缓存内容"
    assert called is False


@pytest.mark.asyncio
async def test_save_note_writes_normalized_key() -> None:
    store = FakeNoteStore()
    source = VideoSource(
        url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
        platform="bilibili",
        video_id="BV1xx411c7mD",
    )
    insight = VideoInsight(
        text="视频笔记正文",
        title="标题",
        duration=30.0,
        frame_count=8,
        subtitle_chars=120,
    )
    await _agent(store)._save_note(source, insight)
    assert store.saved is not None
    assert store.saved["video_key"] == "bilibili:BV1xx411c7mD:p1"
    assert store.saved["note"] == "视频笔记正文"
    assert store.saved["metadata"] == {"warnings": []}


@pytest.mark.asyncio
async def test_save_note_skips_failed_analysis() -> None:
    store = FakeNoteStore()
    source = VideoSource(url="https://example.com/a.mp4", platform="direct")
    await _agent(store)._save_note(source, VideoInsight(text="视频获取失败: timeout"))
    assert store.saved is None
