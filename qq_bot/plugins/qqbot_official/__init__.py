"""QQ 开放平台接入（官方 API）。

接线层：事件 → Incoming → 平台无关的 run_turn。
启用方式：配置 `.env` 的 `QQ_BOTS` 后运行 `./run_bot.sh`。
"""

import asyncio
import logging
import os

from nonebot import get_driver, get_plugin_config, on_message
from nonebot.adapters.qq import (
    Bot,
    C2CMessageCreateEvent,
    GroupMessageCreateEvent,
    MessageSegment,
)
from nonebot.adapters.qq.event import QQMessageEvent
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from qq_bot.chat.turn import run_turn
from qq_bot.platform.qqbot.incoming import (
    from_event as qq_from_event,
)
from qq_bot.platform.qqbot.incoming import (
    is_group_trigger,
)
from qq_bot.platform.qqbot.reply import QQReply
from qq_bot.services.config import AiChatConfig
from qq_bot.services.memory_compress import schedule_compress
from qq_bot.services.memory_writing import get_memory
from qq_bot.services.sticker_sync import sync_stickers

from .commands import handle_command

logger = logging.getLogger(__name__)

__plugin_meta__ = PluginMetadata(
    name="QQ 官方机器人对话",
    description="QQ 开放平台（官方 API）的 AI 对话接入",
    usage="在群里 @机器人 或在私聊中发消息",
    supported_adapters={"~qq"},
)


async def _is_group_at(event: QQMessageEvent) -> bool:
    return is_group_trigger(event)


async def _is_c2c(event: QQMessageEvent) -> bool:
    return isinstance(event, C2CMessageCreateEvent)


def _plain_text(event: QQMessageEvent) -> str:
    try:
        return event.get_message().extract_plain_text().strip()
    except ValueError:
        return ""


async def _is_command(event: QQMessageEvent) -> bool:
    return _plain_text(event).startswith("/")


command_matcher = on_message(rule=Rule(_is_command), priority=5, block=True)
driver = get_driver()


@driver.on_startup
async def _sync_stickers_on_startup() -> None:
    """QQ 模式不加载 ai_chat 插件，启动时手动同步表情库，保证 send_sticker 可用。"""
    try:
        count = await sync_stickers()
        logger.info("QQ 模式表情包库已同步：%s 个", count)
    except Exception:
        logger.exception("QQ 模式表情包同步失败")


_FULL_GROUP = os.getenv("QQ_GROUP_FULL_MESSAGES", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


async def _is_full_group(event: QQMessageEvent) -> bool:
    """只处理未 @ 机器人的普通群消息（@ 消息由 AI matcher 处理，避免重复落库）。"""
    return isinstance(event, GroupMessageCreateEvent) and not is_group_trigger(event)


group_matcher = on_message(rule=Rule(_is_group_at), priority=10, block=True)
c2c_matcher = on_message(rule=Rule(_is_c2c), priority=10, block=True)


async def _dispatch(bot: Bot, event: QQMessageEvent) -> None:
    incoming = qq_from_event(event, reply=QQReply(bot, event))
    if not incoming.text and not incoming.images:
        await bot.send(event, MessageSegment.text("你发了个空消息 我没看懂"))
        return

    config = get_plugin_config(AiChatConfig)
    try:
        await asyncio.wait_for(run_turn(incoming, config=config), timeout=120)
    except asyncio.TimeoutError:
        logger.error("QQ turn timeout after 120s")
    except Exception:
        logger.exception("QQ 官方路径处理失败")
        await bot.send(event, MessageSegment.text("我这边卡了一下 稍后再试"))


async def _record_full_message(bot: Bot, event: QQMessageEvent) -> None:
    """把群内普通消息写入该成员的线程，供群窗口/摘要使用；不触发 AI。"""
    author = getattr(event, "author", None)
    if getattr(author, "bot", False):
        return
    incoming = qq_from_event(event, reply=QQReply(bot, event))
    if not incoming.text and not incoming.images:
        return
    repo = await get_memory()
    if repo is None or incoming.group_key is None:
        return
    uid = incoming.user_key
    gid = incoming.group_key
    if incoming.display_name:
        try:
            await repo.refresh_member_name(
                group_id=gid, user_id=uid, card="", nickname=incoming.display_name
            )
        except Exception:
            logger.debug("QQ 成员名刷新失败", exc_info=True)
    content = incoming.text
    if incoming.images:
        content += f"\n[图片 x{len(incoming.images)}]"
    thread_id = await repo.ensure_thread("group_member", gid, uid)
    name = incoming.display_name or "群成员"
    await repo.append(
        thread_id=thread_id,
        group_id=gid,
        sender_id=uid,
        role="user",
        content=f'<user identity id="{uid}" name="{name}"/>\n{content}',
        sender_name=name,
    )
    schedule_compress(gid)


if _FULL_GROUP:
    full_matcher = on_message(rule=Rule(_is_full_group), priority=7, block=False)

    @full_matcher.handle()
    async def handle_full_message(bot: Bot, event: QQMessageEvent) -> None:
        await _record_full_message(bot, event)


@command_matcher.handle()
async def handle_cmd(bot: Bot, event: QQMessageEvent) -> None:
    repo = await get_memory()
    reply = QQReply(bot, event)
    if repo is None:
        await reply.text("记忆库暂时不可用 稍后再试")
        return
    await handle_command(event, reply, repo)


@group_matcher.handle()
async def handle_group_at(bot: Bot, event: QQMessageEvent) -> None:
    await _dispatch(bot, event)


@c2c_matcher.handle()
async def handle_c2c(bot: Bot, event: C2CMessageCreateEvent) -> None:
    await _dispatch(bot, event)
