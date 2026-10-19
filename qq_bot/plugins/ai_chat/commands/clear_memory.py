from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import to_me

from qq_bot.plugins.ai_chat.memory_writing import get_memory

reset = on_command(
    "reset",
    rule=to_me(),
    aliases={"clear", "重置对话", "清除记忆"},
    block=True,
    force_whitespace=True,
)


@reset.handle()
async def handle_reset(event: MessageEvent, matcher: Matcher) -> None:
    """只清空**当前发言人自己**的线程；同群其他人不受影响（设计 §8 未决项已定）。"""
    memory = await get_memory()
    if memory is None:
        await matcher.finish("记忆库暂时不可用，请稍后再试")

    if isinstance(event, GroupMessageEvent):
        kind, group_id = "group_member", event.group_id
    else:
        kind, group_id = "private", None
    thread_id = await memory.ensure_thread(kind, group_id, event.user_id)
    deleted = await memory.clear_thread(thread_id)
    await matcher.finish(f"已清除你的对话记忆（{deleted} 条），其他人的不受影响")
