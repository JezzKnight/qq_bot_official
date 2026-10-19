"""QQ 官方平台的指令处理（help / reset / usage / model）。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from nonebot import get_plugin_config

from qq_bot.ai.model_registry import get_registry
from qq_bot.chat.commands import parse_command
from qq_bot.chat.pixiv import fetch_pixiv_images
from qq_bot.chat.usage import format_usage, memory_stats_line
from qq_bot.platform.qqbot.incoming import from_event as qq_from_event
from qq_bot.services.client_factory import default_model_name
from qq_bot.services.config import AiChatConfig
from qq_bot.services.cooldown import check_cooldown
from qq_bot.services.session_store import get_session_model, set_session_model

if TYPE_CHECKING:
    from qq_bot.platform.qqbot.reply import QQReply

HELP_TEXT = """能用这些指令
/help 显示本帮助
/reset 清除你自己的对话记忆
/usage 查看今日 token 用量
/model [模型名] 查看或切换模型
/pixiv [类型] 随机来一张 Pixiv 排行榜图片
/whoami 查看机器人记录的你的信息
/reboot 重启机器人（仅管理员）"""


async def handle_command(  # noqa: PLR0911
    event: Any, reply: QQReply, repo: Any
) -> None:
    """解析并执行指令；未知指令给友好提示。"""
    try:
        text = event.get_message().extract_plain_text()
    except ValueError:
        text = ""
    name, args = parse_command(text)

    if name in ("", "help"):
        await reply.passive(HELP_TEXT)
        return

    if name in ("reset", "clear", "重置对话", "清除记忆"):
        await _reset(event, reply, repo)
        return

    if name in ("usage", "用量", "今日用量", "今日token"):
        await _usage(reply, repo)
        return

    if name in ("model", "模型", "切换模型"):
        await _model(event, reply, args)
        return

    if name in ("pixiv", "图来", "来点色图"):
        await _pixiv(event, reply, args)
        return

    if name in ("whoami", "我是谁"):
        await _whoami(event, reply, repo)
        return

    if name in ("reboot", "重启", "重启服务"):
        await _reboot(event, reply)
        return

    await reply.passive(f"没有 /{name} 这个指令 发 /help 看看")


async def _reset(event: Any, reply: QQReply, repo: Any) -> None:
    if repo is None:
        await reply.passive("记忆库暂时不可用 稍后再试")
        return
    incoming = qq_from_event(event, reply=reply)
    uid = incoming.user_key
    gid = incoming.group_key
    thread_id = await repo.ensure_thread(
        "group_member" if gid is not None else "private", gid, uid
    )
    deleted = await repo.clear_thread(thread_id)
    await reply.passive(f"已清除你的对话记忆（{deleted} 条） 其他人不受影响")


async def _usage(reply: QQReply, repo: Any) -> None:
    if repo is None:
        await reply.passive("记忆库暂时不可用 稍后再试")
        return
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    summary = await repo.usage_summary(today)
    lines = format_usage(summary, today, today)
    memory_line = await memory_stats_line(repo)
    if memory_line:
        lines.extend(["", memory_line])
    await reply.passive("\n".join(lines))


async def _model(event: Any, reply: QQReply, arg: str) -> None:
    incoming = qq_from_event(event, reply=reply)
    uid = incoming.user_key
    gid = incoming.group_key
    session_id = f"group_{gid}" if gid is not None else f"user_{uid}"
    names = get_registry().names()
    arg = arg.strip()

    if arg in ("", "list"):
        current = get_session_model(session_id, default_model_name())
        lines = ["可用模型："]
        lines.extend(f"  {name}{' ←当前' if name == current else ''}" for name in names)
        lines.append("输入 /model <模型名> 切换")
        await reply.passive("\n".join(lines))
        return

    if arg not in names:
        await reply.passive(f"没有这个模型。可选：{', '.join(names)}")
        return

    set_session_model(session_id, arg)
    await reply.passive(f"已切换到 {arg}")


async def _reboot(event: Any, reply: QQReply) -> None:
    incoming = qq_from_event(event, reply=reply)
    config = get_plugin_config(AiChatConfig)
    admins = {x.strip() for x in config.admin_openids.split(",") if x.strip()}
    if not admins:
        await reply.passive("未配置 ADMIN_OPENIDS，无法执行重启")
        return
    if incoming.user_key not in admins:
        await reply.passive("你没有权限执行此操作")
        return
    await reply.passive("正在重启...")
    await asyncio.sleep(0.5)
    os._exit(0)


async def _pixiv(event: Any, reply: QQReply, arg: str) -> None:
    incoming = qq_from_event(event, reply=reply)
    config = get_plugin_config(AiChatConfig)
    if not check_cooldown(f"pixiv_{incoming.user_key}", config.cooldown_seconds):
        await reply.passive(f"太快啦 请 {config.cooldown_seconds} 秒后再试")
        return

    cache_dir = Path(os.getenv("PIXIV_CACHE_DIR", "data/data/ai_chat/pixiv"))
    result = await fetch_pixiv_images(
        arg.strip(),
        cache_dir=cache_dir,
        proxy=config.proxy,
        refresh_tokens=config.refresh_token,
    )
    if isinstance(result, str):
        await reply.passive(result)
        return

    await reply.passive(f"找到 {len(result)} 张")
    for img in result:
        if not await reply.image(img):
            break


async def _whoami(event: Any, reply: QQReply, repo: Any) -> None:
    """展示机器人当前掌握的本人信息（openid 只显示尾号）。"""
    if repo is None:
        await reply.passive("记忆库暂时不可用 稍后再试")
        return
    incoming = qq_from_event(event, reply=reply)
    uid = incoming.user_key
    gid = incoming.group_key
    preferred = await repo.preferred_name(uid)
    names = await repo.list_names(gid)
    mine = [n["name"] for n in names if n["user_id"] == uid]
    lines = [
        f"openid 尾号: {incoming.user_key[-8:]}",
        f"内部 id: {uid}",
        f"显示名: {incoming.display_name or '未知'}",
        f"期望称呼: {preferred or '未设置'}",
        f"已知别名: {', '.join(mine) or '无'}",
    ]
    await reply.passive("\n".join(lines))
