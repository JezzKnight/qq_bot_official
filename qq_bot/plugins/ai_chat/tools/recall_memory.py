# ruff: noqa: E501   # 工具描述含中文长句，行宽超过 88 属有意为之
"""成员长期记忆查询工具：按需读取指定成员的长期记忆（L3）。"""

from qq_bot.plugins.ai_chat.memory_writing import get_memory

from .context import current_scope
from .registry import register_tool


@register_tool(
    name="recall_memory",
    description=(
        "读取指定成员的长期记忆。\n\n"
        "## 何时调用\n"
        "动态注入只覆盖：群公共记忆、当前发言人、本条消息按名字匹配到的成员。"
        "当你需要了解某个【其他】成员（既非当前发言人、也未被本条消息提及）的背景，"
        "且这份信息可能影响回答时，调用本工具查询该成员的长期记忆。\n\n"
        "## 参数\n"
        "target_uid：目标成员的 user_id，"
        "从对话中 <user identity> 标签的 id 属性或群成员列表 XML 中获取。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_uid": {
                "type": "string",
                "description": "目标成员的 user_id，从 <user identity> 标签 id 属性或群成员列表获取",
            }
        },
        "required": ["target_uid"],
    },
)
async def recall_memory(target_uid: str) -> str:
    """读取指定成员的个人长期记忆"""
    scope = current_scope.get() or ""
    if not scope.startswith("groups/"):
        return "该工具仅群聊可用"
    gid = int(scope.split("/")[1])
    repo = await get_memory()
    if repo is None:
        return "记忆库暂时不可用"
    try:
        uid = int(target_uid)
    except ValueError:
        return f"target_uid 不是合法数字: {target_uid}"

    rows = await repo.list_memories(user_id=uid, group_id=gid)
    entries = [r for r in rows if r["scope_kind"] == "user"]
    if not entries:
        return f"成员 {target_uid} 暂无长期记忆"
    header = f'## <user id="{target_uid}"/> 的个人记忆'
    lines = [header]
    lines.extend(f"[{r['key']}] {r['content']}".rstrip() for r in entries)
    return "\n".join(lines)
