from pathlib import Path

from nonebot import get_plugin_config, on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.rule import to_me
from nonebot_plugin_localstore import get_plugin_data_dir

from qq_bot.chat.pixiv import ALLOWED_MODES, fetch_pixiv_images
from qq_bot.plugins.ai_chat.commands.cooldown import check_cooldown
from qq_bot.plugins.ai_chat.config import AiChatConfig

pixiv = on_command(
    "pixiv", rule=to_me(), aliases={"图来", "来点色图"}, block=True,
    force_whitespace=True,
)


@pixiv.handle()
async def handle_pixiv(event: MessageEvent, matcher: Matcher) -> None:
    config = get_plugin_config(AiChatConfig)
    if not check_cooldown(f"pixiv_{event.user_id}", config.cooldown_seconds):
        await matcher.finish(f"太快啦，请{config.cooldown_seconds}秒后再试")

    parts = event.get_plaintext().split(maxsplit=1)
    mode = parts[1].strip() if len(parts) > 1 else ""
    if mode not in ALLOWED_MODES:
        await matcher.finish("无效参数")

    result = await fetch_pixiv_images(
        mode,
        cache_dir=Path(get_plugin_data_dir()) / "pixiv",
        proxy=config.proxy,
        refresh_tokens=config.refresh_token,
    )
    if isinstance(result, str):
        await matcher.finish(result)
    for img in result:
        await matcher.send(MessageSegment.image(img))
    await matcher.finish()
