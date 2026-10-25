"""系统提示词组装：只负责顺序与拼接，不依赖任何平台。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SystemPromptParts:
    """各段内容由调用方准备好；空字符串会被跳过。"""

    system: str
    group: str
    sticker: str
    summary: str
    speaker: str
    memory: str
    time: str
    mode: str = ""


def build_system_prompt(parts: SystemPromptParts) -> str:
    """按固定顺序拼接，保持稳定前缀以命中模型侧 prompt 缓存。"""
    return "\n\n".join(
        chunk
        for chunk in (
            parts.system,
            parts.group,
            parts.mode,
            parts.sticker,
            parts.summary,
            parts.speaker,
            parts.memory,
            parts.time,
        )
        if chunk
    )
