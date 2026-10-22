"""把字幕 + 关键帧交给视觉模型，产出可读的视频理解结果。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from prompts.service import prompt_service
from qq_bot.ai.types import ChatMessage, ImageData

if TYPE_CHECKING:
    from .types import VideoFrame, VideoSource

logger = logging.getLogger(__name__)

UsageRecorder = Callable[[int, int, int], Awaitable[None]]
_MIN_MAX_TOKENS = 1600


def _reasoning_effort(model: str) -> str | None:
    """DeepSeek V4 默认 high；视频理解只需要低推理预算。"""
    return "low" if "deepseek" in model.lower() else None


def _format_frame_times(frames: list[VideoFrame]) -> str:
    parts = []
    for frame in frames:
        minute, second = divmod(int(frame.timestamp), 60)
        parts.append(f"{minute:02d}:{second:02d}")
    return "、".join(parts)


def _build_user_prompt(  # noqa: PLR0913 —— 视觉提示词需要多段上下文
    *,
    source: VideoSource,
    title: str,
    duration: float,
    transcript: str,
    subtitle_lang: str,
    frames: list[VideoFrame],
    question: str,
    description: str,
) -> str:
    minute, second = divmod(int(duration), 60)
    lines = [
        f"用户问题：{question or '请总结这个视频的内容'}",
        f"视频来源：{source.platform} {source.url}",
        f"标题：{title or '未知'}",
        f"时长：{minute:02d}:{second:02d}",
        f"简介：{description[:800] or '无'}",
        f"关键帧时间点：{_format_frame_times(frames)}",
        (
            f"字幕（{subtitle_lang}）：\n{transcript}"
            if transcript
            else "字幕：无（没有拿到 CC/AI 字幕，不要假装听见了音频）"
        ),
        (
            "请结合关键帧和字幕回答，按以下结构输出：\n"
            "1. 一句话总结；\n"
            "2. 时间线（只写有证据的时间点）；\n"
            "3. 针对用户问题的回答；\n"
            "4. 不确定或缺失的信息。\n"
            "不要编造画面里没有的内容；看不清就说不确定。"
        ),
    ]
    return "\n\n".join(lines)


async def analyze_frames(  # noqa: PLR0913 —— 模型调用参数较多
    *,
    client: Any,
    model: str,
    source: VideoSource,
    title: str,
    duration: float,
    description: str,
    transcript: str,
    subtitle_lang: str,
    frames: list[VideoFrame],
    question: str,
    usage_recorder: UsageRecorder | None = None,
    max_tokens: int = 2400,
) -> str:
    system_prompt = prompt_service.get_agent_prompt("video")
    user_prompt = _build_user_prompt(
        source=source,
        title=title,
        duration=duration,
        transcript=transcript,
        subtitle_lang=subtitle_lang,
        frames=frames,
        question=question,
        description=description,
    )
    images = [ImageData(data=f.jpeg, mine_type="image/jpeg") for f in frames]
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_prompt, images=images or None),
    ]

    async def _call(tokens: int) -> str:
        response = await client.chat(
            messages=messages,
            model=model,
            temperature=0.3,
            max_tokens=max(tokens, _MIN_MAX_TOKENS),
            reasoning_effort=_reasoning_effort(model),
        )
        if usage_recorder is not None:
            await usage_recorder(
                response.prompt_tokens,
                response.cached_tokens,
                response.completion_tokens,
            )
        return (response.content or "").strip()

    text = await _call(max_tokens)
    if text:
        return text
    # 推理模型可能把预算全花在 reasoning 上，扩大一次预算再试。
    text = await _call(max_tokens * 2)
    if text:
        return text
    msg = "视觉模型没有返回内容"
    raise RuntimeError(msg)
