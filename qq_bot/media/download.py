"""按 URL 下载图片并预处理为可交给视觉模型的二进制。"""

from __future__ import annotations

import logging

import httpx

from qq_bot.ai.image_prep import shrink_image_for_vision, sniff_image_mime
from qq_bot.ai.types import ImageData

logger = logging.getLogger(__name__)

_HTTP_OK = 200


async def download_image(url: str) -> ImageData | None:
    """从图片 URL 下载二进制数据，失败返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(url)
            if resp.status_code == _HTTP_OK:
                content_type = resp.headers.get("content-type", "image/jpeg")
                # 图床可能给出泛化/错误的 content-type，以字节魔数为准
                content_type = sniff_image_mime(resp.content, fallback=content_type)
                data, content_type = shrink_image_for_vision(resp.content, content_type)
                return ImageData(data=data, mine_type=content_type)
    except Exception:  # noqa: BLE001 —— 下载失败不应影响对话主流程
        logger.info("图片下载失败")
        return None
    return None
