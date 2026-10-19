"""查询 token 用量 —— 用户命令 /usage [YYYY-MM-DD]，不带日期默认查询今日"""
from datetime import date, datetime
from typing import NoReturn

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.rule import to_me

from qq_bot.chat.usage import format_usage, memory_stats_line
from qq_bot.plugins.ai_chat.memory_writing import get_memory

usage_cmd = on_command(
    "usage",
    rule=to_me(),
    aliases={"用量", "今日用量", "今日token"},
    block=True,
    force_whitespace=True,
)


def _parse_day(text: str) -> str | None:
    """校验并规范化 YYYY-MM-DD 格式，无效返回 None"""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


@usage_cmd.handle()
async def handle_usage(matcher: Matcher, args: Message = CommandArg()) -> NoReturn:
    """查询 token 用量：带日期查指定日期，不带日期查今天"""
    today_str = datetime.now().astimezone().strftime("%Y-%m-%d")

    arg = args.extract_plain_text().strip()
    if arg:
        day = _parse_day(arg)
        if day is None:
            await matcher.finish("📅 格式应为 YYYY-MM-DD，例：/usage 2026-08-07")
    else:
        day = today_str

    repo = await get_memory()
    if repo is None:
        await matcher.finish("❌ 记忆库暂时不可用，稍后再试。")
    try:
        summary = await repo.usage_summary(day)
    except Exception:  # noqa: BLE001
        await matcher.finish("❌ 用量统计服务暂不可用，请稍后重试。")

    lines = format_usage(summary, day, today_str)

    memory_line = await memory_stats_line(repo)
    if memory_line:
        lines.extend(["", memory_line])

    await matcher.finish("\n".join(lines))
