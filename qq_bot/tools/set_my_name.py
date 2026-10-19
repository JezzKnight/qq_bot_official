# ruff: noqa: E501   # 工具描述含中文长句，行宽超过 88 属有意为之
"""称呼设置工具：用户明确要求「以后叫我 X」时写入 user_names。"""

from qq_bot.memory.naming import set_my_name as apply_name
from qq_bot.services.memory_writing import get_memory

from .context import current_scope
from .registry import register_tool


@register_tool(
    name="set_my_name",
    description=(
        "设置【当前用户】希望被称呼的名字。\n\n"
        "## 何时调用\n"
        "只有当用户明确表达「以后叫我 X」「叫我 X」「你可以叫我 X」「别叫我原来的名字」时调用。\n"
        "用户只是在描述别人、或随口提到某个称呼时，不要调用。\n\n"
        "## 边界\n"
        "- 只能给当前发言人本人设置，不能给别人设置称呼。\n"
        "- 名字不做字符合法性限制，用户想叫什么就叫什么。\n"
        "- 名字与他人重复时会被拒绝：只把拒绝原因告诉用户，不要替他发明替代名字。\n"
        "- 用户说「不用叫这个名字了 / 叫我原来的名字」时，name 传空字符串。\n\n"
        "## 返回\n"
        "把返回的文案转述给用户即可（成功 / 已被占用 / 就是当前称呼 / 已取消）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "用户希望被称呼的名字；传空字符串表示取消称呼、回落到群名片",
            }
        },
        "required": ["name"],
    },
)
async def set_my_name(name: str) -> str:
    """设置当前用户的称呼"""
    scope = current_scope.get() or ""
    repo = await get_memory()
    if repo is None:
        return "记忆库暂时不可用，稍后再试"

    if scope.startswith("groups/"):
        group_id: str | None = scope.split("/")[1]
        user_id = scope.split("/")[2]
    elif scope.startswith("private/"):
        group_id = None
        user_id = scope.split("/")[1]
    else:
        return "无法确定当前会话，稍后再试"

    result = await apply_name(repo, user_id=user_id, group_id=group_id, name=name)
    return result.message
