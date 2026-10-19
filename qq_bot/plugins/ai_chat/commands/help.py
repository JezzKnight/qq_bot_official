"""帮助指令：列出全部指令，或查看单条指令的详细用法。"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import to_me

# 指令注册表：所有可用的 / 指令集中在这里维护
# args: 参数占位；desc: 一句话说明；aliases: 别名；params: 参数取值说明
COMMANDS: dict[str, dict] = {
    "pixiv": {
        "args": "<类型>",
        "desc": "随机获取 Pixiv 排行榜图片",
        "aliases": ["图来", "来点色图"],
        "params": [
            {"name": "day", "desc": "日榜（默认）"},
            {"name": "week", "desc": "周榜"},
            {"name": "day_male", "desc": "男性向日榜"},
            {"name": "week_original", "desc": "原创周榜"},
            {"name": "week_rookie", "desc": "新人日榜"},
            {"name": "day_male_r18", "desc": "男性向 R18 日榜"},
            {"name": "week_r18", "desc": "R18 周榜"},
        ],
    },
    "reset": {
        "args": "",
        "desc": "清除你自己的对话记忆（其他人不受影响）",
        "aliases": ["clear", "重置对话", "清除记忆"],
    },
    "model": {
        "args": "[模型名]",
        "desc": "查看可用模型，或切换到指定模型",
        "aliases": ["模型", "切换模型"],
        "params": [
            {"name": "list", "desc": "列出当前可用模型（默认行为）"},
            {"name": "<模型名>", "desc": "切换到该模型"},
        ],
    },
    "usage": {
        "args": "[YYYY-MM-DD]",
        "desc": "查看 token 用量与记忆库规模",
        "aliases": ["用量", "今日用量", "今日token"],
        "params": [{"name": "YYYY-MM-DD", "desc": "留空查今天"}],
    },
    "reminders": {
        "args": "",
        "desc": "查看你设置过、还没触发的提醒",
        "aliases": ["提醒列表", "我的提醒", "定时任务列表"],
    },
    "comfyui": {
        "args": "",
        "desc": "本地 ComfyUI 生图：把图片和指令一起发给我",
        "aliases": [],
    },
    "reboot": {
        "args": "",
        "desc": "重启 bot（仅管理员可用）",
        "aliases": ["重启", "重启服务"],
    },
    "help": {
        "args": "[指令名]",
        "desc": "显示指令列表，或查看单条指令的详细用法",
        "aliases": [],
    },
}

help_cmd = on_command(
    "help", rule=to_me(), aliases={""}, block=True, force_whitespace=True
)


def _usage_line(name: str, info: dict) -> str:
    args = info.get("args") or ""
    head = f"/{name} {args}".rstrip()
    return f"{head} — {info['desc']}"


def _find(name: str) -> tuple[str, dict] | None:
    """按指令名或别名查找；都不匹配返回 None。"""
    key = name.strip().lower()
    if key in COMMANDS:
        return key, COMMANDS[key]
    for cmd, info in COMMANDS.items():
        if key in {a.lower() for a in info.get("aliases", [])}:
            return cmd, info
    return None


def _detail_text(name: str, info: dict) -> str:
    lines = [f"/{name} {info.get('args', '')}".rstrip(), f"说明: {info['desc']}"]
    if info.get("aliases"):
        lines.append("别名: " + " / ".join(info["aliases"]))
    if info.get("params"):
        lines.append("参数:")
        lines.extend(f"  {p['name']} = {p['desc']}" for p in info["params"])
    return "\n".join(lines)


def _list_text() -> str:
    lines = ["可用指令（发送时记得 @ 我）:", ""]
    lines.extend(_usage_line(name, info) for name, info in COMMANDS.items())
    lines.extend(["", "输入 /help <指令名> 查看单条详情"])
    return "\n".join(lines)


@help_cmd.handle()
async def handle_help(event: MessageEvent, matcher: Matcher) -> None:
    arg = event.get_plaintext().strip().replace("/help", "").strip()
    if not arg:
        await matcher.finish(_list_text())

    found = _find(arg)
    if found is None:
        await matcher.finish(f"没有 /{arg} 这个指令，发 /help 看看全部指令")
    name, info = found
    await matcher.finish(_detail_text(name, info))
