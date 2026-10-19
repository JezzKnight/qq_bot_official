import os

import nonebot
from pathlib import Path

from dotenv import load_dotenv
from nonebot import get_plugin_config, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from qq_bot.logging_config import init_logging

# NoneBot 只把 .env 读进 Config，不会写入 os.environ；而 reboot 的管理员白名单
# 与 model.yaml 的 api_key_env 都依赖 os.getenv。这里显式导出。
# 必须放在 .commands 导入之前——reboot.py 在导入期就读取 ADMIN_USERS。
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_ENV_FILE if _ENV_FILE.is_file() else None)

_PLATFORM = os.getenv("PLATFORM", "onebot").strip().lower()


def _should_register() -> bool:
    """只在 NoneBot 已初始化且非 QQ 模式时注册 matcher/指令。

    - 测试直接 import 本包（未 init）时不注册，避免 on_command 报错；
    - PLATFORM=qq 时本包只提供共享服务，不注册任何 matcher。
    """
    if _PLATFORM == "qq":
        return False
    try:
        nonebot.get_driver()
    except ValueError:
        return False
    return True


_REGISTER = _should_register()

# PLATFORM=qq 时本插件只作为"共享服务提供者"被 qqbot_official 引用，
# 不注册任何 matcher/指令：两套适配器的事件字段不兼容，同时注册会互相误触发。
if _REGISTER:
    from .commands import (
        clear_memory,
        comfyui,
        get_pixiv,
        help,
        list_reminders,
        reboot,
        switch_model,
        usage,
    )
    from .handler import handle_ai_chat

from .config import AiChatConfig

# 日志初始化与 matcher 注册无关：QQ 模式也要写日志文件
if _REGISTER or _PLATFORM == "qq":
    init_logging()


async def keyword_rule(event: Event) -> bool:
    """
    关键词触发实现
    """
    config = get_plugin_config(AiChatConfig)
    if not isinstance(event, MessageEvent):
        return False
    # 获取event传来的消息然后消除前后空格以及小写
    text = event.get_plaintext().strip().lower()
    # 循环配置里写的关键词然后当关键词在text中被提及的时候any返回True
    return any(kw.lower() in text for kw in config.trigger_keywords)


async def at_me_rule(event: Event) -> bool:
    """任意位置 @机器人 即触发。

    适配器自带 to_me() 只检查消息首/尾段，@ 出现在中间（或尾部带非空白
    内容）时会漏触发导致静默不响应。此规则先沿用适配器原判定（私聊恒触发、
    首/尾 @、昵称唤起），再扫描消息全部段位兜底。
    """
    if not isinstance(event, MessageEvent):
        return False
    if event.is_tome():
        return True
    return any(
        seg.type == "at" and str(seg.data.get("qq", "")) == str(event.self_id)
        for seg in event.message
    )


if _REGISTER:
    # nonebot中标准注册事件响应器
    ai_chat = on_message(
        rule=Rule(at_me_rule),
        priority=10,
        block=False,
    )

    @ai_chat.handle()
    async def handle(event: MessageEvent, matcher: Matcher) -> None:
        await handle_ai_chat(event, matcher)


__plugin_meta__ = PluginMetadata(
    name="AI 对话",
    description="基于 OpenAI 兼容接口的 AI 对话插件",
    usage="@我 或 包含关键词的消息触发 AI 对话",
    config=AiChatConfig,
    supported_adapters={"~onebot.v11"},
)

