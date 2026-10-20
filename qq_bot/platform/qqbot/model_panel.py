"""模型切换面板：QQ 官方 Markdown + InlineKeyboard。"""

from __future__ import annotations

from nonebot.adapters.qq.models.common import (
    Action,
    Button,
    InlineKeyboard,
    InlineKeyboardRow,
    MessageKeyboard,
    RenderData,
)


def build_model_keyboard(names: list[str], current: str) -> MessageKeyboard:
    """把模型列表做成一行一个按钮的 QQ 键盘面板。"""
    rows = []
    for name in names:
        label = f"{name} ✓" if name == current else name
        rows.append(
            InlineKeyboardRow(
                buttons=[
                    Button(
                        render_data=RenderData(
                            label=label, visited_label=label, style=1
                        ),
                        action=Action(type=2, data=f"model:{name}"),
                    )
                ]
            )
        )
    return MessageKeyboard(content=InlineKeyboard(rows=rows))
