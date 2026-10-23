"""QQ 开放平台接入（官方 API）。

接线层：事件 → Incoming → 平台无关的 run_turn。
启用方式：配置 `.env` 的 `QQ_BOTS` 后运行 `./run_bot.sh`。
"""

import asyncio
import logging
import os

from nonebot import get_driver, get_plugin_config, on_message, on_notice
from nonebot.adapters.qq import (
    Bot,
    C2CMessageCreateEvent,
    GroupMessageCreateEvent,
    InteractionCreateEvent,
    MessageSegment,
)
from nonebot.adapters.qq.event import QQMessageEvent
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from qq_bot.ai.model_registry import get_registry
from qq_bot.chat import passive_window
from qq_bot.chat.autotrigger import maybe_autotrigger, record_bot_message
from qq_bot.chat.turn import run_turn
from qq_bot.memory.memory_config import get_memory_config
from qq_bot.platform.ports import Incoming
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
from qq_bot.services.session_store import set_session_model
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


async def _is_model_button(event: InteractionCreateEvent) -> bool:
    data = event.data.resolved.button_data or ""
    return data.startswith("model:")


model_button_matcher = on_notice(rule=Rule(_is_model_button), priority=5, block=True)
driver = get_driver()


@driver.on_startup
async def _sync_stickers_on_startup() -> None:
    """QQ 模式不加载 ai_chat 插件，启动时手动同步表情库，保证 send_sticker 可用。"""
    try:
        count = await sync_stickers()
        logger.info("QQ 模式表情包库已同步：%s 个", count)
        logger.info(
            "群摘要触发阈值：%s 条",
            get_memory_config().compression.trigger_turns,
        )
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
    # 登记被动窗口：@ 这一轮按最保守口径记账（假设 4 段已用满），窗口里只留
    # 1 次给自动接话。真实剩余额度以平台为准：宁可少说，也不能超发被拒。
    if incoming.group_key:
        passive_window.register(
            incoming.group_key,
            incoming.reply,
            window_seconds=float(config.group_autotrigger_window_seconds),
        )
    try:
        final_content = await asyncio.wait_for(
            run_turn(incoming, config=config), timeout=120
        )
    except asyncio.TimeoutError:
        logger.exception("QQ turn timeout after 120s")
    except Exception:
        logger.exception("QQ 官方路径处理失败")
        await bot.send(event, MessageSegment.text("我这边卡了一下 稍后再试"))
    else:
        # 记下 bot 上一句：自动接话靠「实词重叠」判断是不是同一个话题
        if incoming.group_key:
            record_bot_message(incoming.group_key, final_content or "")


async def _record_full_message(
    bot: Bot,
    event: QQMessageEvent,
    *,
    incoming: Incoming | None = None,
) -> None:
    """把群内普通消息写入该成员的线程，供群窗口/摘要使用；不触发 AI。

    ``incoming`` 允许调用方传入已构造好的对象：同一条消息既要落库又要判自动
    接话，构造两次纯属浪费。
    """
    author = getattr(event, "author", None)
    if getattr(author, "bot", False):
        return
    if incoming is None:
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
        config = get_plugin_config(AiChatConfig)
        incoming = qq_from_event(event, reply=QQReply(bot, event))
        await _record_full_message(bot, event, incoming=incoming)
        if config.group_autotrigger:
            await maybe_autotrigger(bot, event, config=config, incoming=incoming)


@command_matcher.handle()
async def handle_cmd(bot: Bot, event: QQMessageEvent) -> None:
    repo = await get_memory()
    reply = QQReply(bot, event)
    # 不在这里拦截 None：/reboot 不依赖记忆库，仍然可用
    await handle_command(event, reply, repo)


@model_button_matcher.handle()
async def handle_model_button(bot: Bot, event: InteractionCreateEvent) -> None:
    data = event.data.resolved.button_data or ""
    model = data.partition(":")[2]

    if model not in get_registry().names():
        await bot.put_interaction(interaction_id=event.id, code=1)
        await bot.send(event, MessageSegment.text("这个模型不在了"))
        return

    if event.group_openid:
        session_id = f"group_{event.group_openid}"
    elif event.user_openid:
        session_id = f"user_{event.user_openid}"
    else:
        await bot.put_interaction(interaction_id=event.id, code=1)
        await bot.send(event, MessageSegment.text("无法确定当前会话"))
        return

    set_session_model(session_id, model)
    # ACK 结束客户端 loading；面板本身不删，由平台保留
    await bot.put_interaction(interaction_id=event.id, code=0)
    await bot.send(event, MessageSegment.text(f"已切换到 {model}"))


@group_matcher.handle()
async def handle_group_at(bot: Bot, event: QQMessageEvent) -> None:
    await _dispatch(bot, event)


@c2c_matcher.handle()
async def handle_c2c(bot: Bot, event: C2CMessageCreateEvent) -> None:
    await _dispatch(bot, event)
