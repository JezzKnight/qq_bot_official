import logging
from typing import cast

from qq_bot.chat.turn import run_turn

from .config import AiChatConfig

logger = logging.getLogger(__name__)
import nonebot
from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11 import (
    Bot,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot_plugin_localstore import get_plugin_data_dir

from qq_bot.platform.onebot.incoming import from_event as onebot_from_event
from qq_bot.platform.onebot.reply import OneBotReply

from .session_store import _load_session_models
from .utils import (
    sync_group_members,
)

# 加载保存的谁使用什么模型的信息
_load_session_models()
# ──────── 主函数 ────────
async def handle_ai_chat(event: MessageEvent, matcher: Matcher) -> None:
    """适配器接线：OneBot 事件 → Incoming → 平台无关的 run_turn。"""
    bot = cast(Bot, nonebot.get_bot())
    incoming = onebot_from_event(
        event, reply=OneBotReply(matcher), self_key=str(bot.self_id)
    )

    # 空消息（既无文字也无图片/视频）回固定表情包，与旧路径一致
    if not incoming.text and not incoming.images:
        sticker_path = get_plugin_data_dir() / "stickers" / "what.jpg"
        await matcher.finish(MessageSegment.image(file=sticker_path.read_bytes()))
        return

    config = get_plugin_config(AiChatConfig)

    async def _bootstrap() -> bool:
        return await sync_group_members(bot, event, config.bot_self_id)

    await run_turn(incoming, config=config, bootstrap_members=_bootstrap)
