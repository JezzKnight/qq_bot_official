import re
import xml.etree.ElementTree as ET

import httpx
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent

from qq_bot.media.download import download_image
from qq_bot.media.video import video_urls_in_text
from qq_bot.memory.naming import escape_for_prompt

from ...ai.types import ImageData
from .memory_writing import get_memory


async def sync_group_members(
    bot: Bot, event: GroupMessageEvent, bot_self_id: str = ""
) -> bool:
    """全量拉取群成员并写入 PG（users + group_members + 名片别名），排除 bot 自身。

    仅用于"成员表为空时的一次性引导"：日常改名由 handler 的增量刷新覆盖，
    因此不再提供 /scan 指令。
    """
    try:
        members = await bot.get_group_member_list(group_id=event.group_id)
    except Exception as e:
        print(f"[ERROR] 获取群成员列表失败: {e}")
        return False

    exclude_id = bot_self_id.strip() or str(bot.self_id)
    if exclude_id:
        members = [m for m in members if str(m.get("user_id", "")) != exclude_id]

    repo = await get_memory()
    if repo is None:
        print("[WARN] 记忆库不可用，群成员未保存")
        return False
    await repo.upsert_members(event.group_id, members)
    return True


async def load_group_members_list(group_id: int | str) -> list[dict] | None:
    """兼容层：从 PG 读群成员；无成员返回 None。"""
    from qq_bot.memory.members import members_list

    return await members_list(await get_memory(), int(group_id))


async def get_group_members(group_id: int | str) -> str | None:
    """兼容层：构造群成员 XML（带 call 属性）。"""
    from qq_bot.memory.members import members_xml

    return await members_xml(await get_memory(), int(group_id))


def split_message(text: str) -> list[str]:
    """切分回复消息，用于实现分段多发"""
    chunks = []
    for msg in text.split("\n\n"):
        if msg:
            chunks.append(msg)
        else:
            continue
    return chunks


async def extract_images(event: MessageEvent) -> list[ImageData]:
    """提取图片二进制：直接消息 + 被引用(reply)消息中的图片。

    复用 download_image 逐张下载。OneBot 适配器已把被引用消息抓到
    event.reply.message（结构化 Message，与普通消息同构，image 段带 url），
    并入遍历目标后，引用图片与直发图片走同一链路被模型读到。
    """
    segments = list(event.get_message())
    reply = getattr(event, "reply", None)
    if reply is not None:
        segments.extend(reply.message)
    images: list[ImageData] = []
    for seg in segments:
        if seg.type == "image":
            img_url = seg.data.get("url")
            if not img_url:
                continue
            img = await download_image(img_url)
            if img:
                images.append(img)
    return images


def extract_image_urls(event: MessageEvent) -> list[str]:
    """提取消息中的图片 URL 列表（不下载，用于注入上下文让主模型触发视觉工具）"""
    return [
        seg.data["url"]
        for seg in event.get_message()
        if seg.type == "image" and seg.data.get("url")
    ]


def extract_video_urls(event: MessageEvent) -> list[str]:
    """提取消息中的视频 URL，用于透传给模型（image_url.url 传公开可访问的视频地址）。

    来源：① QQ 视频消息段（video 段的 url 字段）；② 文本里带视频文件扩展名的链接。
    """
    urls: list[str] = []
    for seg in event.get_message():
        if seg.type == "video":
            u = seg.data.get("url")
            if u:
                urls.append(str(u))
        elif seg.type == "text":
            urls.extend(video_urls_in_text(seg.data.get("text", "") or ""))
    return urls
