# ruff: noqa: E501   # 工具描述含中文长句，行宽超过 88 属有意为之
"""表情包发送工具：把选中的表情包排入本轮回复（文字之后发出）。"""

from qq_bot.memory.stickers import resolve
from qq_bot.services.memory_writing import get_memory
from qq_bot.services.sticker_sync import stickers_dir

from .context import current_pending_sticker
from .registry import register_tool


@register_tool(
    name="send_sticker",
    description=(
        "发送一个表情包。\n\n"
        "## 何时调用\n"
        "当对话情绪或场景与某个表情包的描述匹配时调用，例如无语、震惊、被要求跳舞、讨论食物。"
        "不要每轮都发，只在真正贴合语境时用。\n\n"
        "## 规则\n"
        "- 每轮最多一次；已安排过再调用会被拒绝。\n"
        "- 表情包会在你本轮的文字回复**之后**发出，所以文字里不要写「（发了个表情）」之类的话。\n"
        "- name 用表情包目录里的名称；也接受标签（如「无语」）。\n"
        "- 部分表情需要配合台词，按目录里的描述在正文中说出来。\n"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "表情包名称（或标签），取自本轮提示词里的表情包目录",
            }
        },
        "required": ["name"],
    },
)
async def send_sticker(name: str) -> str:
    """安排一个表情包在本轮回复末尾发送"""
    if current_pending_sticker.get() is not None:
        return "本轮已经安排过一个表情包了，不再重复发送"

    repo = await get_memory()
    if repo is None:
        return "表情包库暂时不可用"

    row = await repo.get_sticker_by_name(name)
    if row is None:
        row = resolve(await repo.list_stickers(), name)
    if row is None:
        return f"没有找到名为「{name}」的表情包"

    base = stickers_dir().resolve()
    path = (base / str(row["path"])).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return "该表情包文件不可用"

    current_pending_sticker.set((str(row["name"]), path.read_bytes()))
    await repo.bump_sticker_usage(int(row["id"]))
    return f"已安排在本轮文字回复之后发送「{row['name']}」"
