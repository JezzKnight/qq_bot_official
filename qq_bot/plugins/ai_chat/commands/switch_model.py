from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import to_me

from qq_bot.ai.model_registry import get_registry

from ..client_factory import default_model_name
from ..session_store import get_session_model, set_session_model

model_cmd = on_command(
    "model",
    rule=to_me(),
    aliases={"切换模型", "模型"},
    block=True,
    force_whitespace=True
)

def _available_names() -> list[str]:
    """可用模型名列表（来自 model.yaml，顺序即声明顺序）"""
    return get_registry().names()


@model_cmd.handle()
async def handle_model(event: MessageEvent, matcher: Matcher):
    if isinstance(event, GroupMessageEvent):
        session_id = f"group_{event.group_id}"
    else:
        session_id = f"user_{event.user_id}"

    arg = event.get_plaintext().strip().replace("/model", "").strip()
    names = _available_names()

    # /model 或 /model list →列出可用模型
    if arg in {"", "list"}:
        current = get_session_model(session_id, default_model_name())
        lines = ["可用模型："]
        for m in names:
            mark = " ←当前" if m == current else ""
            lines.append(f"  {m}{mark}")
        lines.append("\n输入 /model <模型名> 切换")
        await matcher.finish("\n".join(lines))

    # /model <模型名> →切换
    if arg not in names:
        await matcher.finish(f"没有这个模型。可选：{', '.join(names)}")

    set_session_model(session_id, arg)
    await matcher.finish(f"已切换到 {arg}")
