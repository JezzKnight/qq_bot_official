"""视频笔记缓存接口。

这里只定义最小协议，避免视频服务直接依赖 PostgreSQL 仓库实现。
生产实现是 ``PgRepository``；测试可以注入内存假实现。
"""

from __future__ import annotations

from typing import Any, Protocol


class VideoNoteStore(Protocol):
    """按规范化视频键存取分析笔记。"""

    async def get_video_note(self, video_key: str) -> dict[str, Any] | None: ...

    async def save_video_note(  # noqa: PLR0913
        self,
        *,
        video_key: str,
        platform: str,
        video_id: str,
        source_url: str,
        title: str,
        duration: float,
        note: str,
        frame_count: int = 0,
        subtitle_chars: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...
