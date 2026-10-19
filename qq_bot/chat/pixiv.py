"""Pixiv 排行榜取图（平台无关）：只负责拿图片字节，发送由调用方处理。"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import logging

import httpx

logger = logging.getLogger(__name__)

ALLOWED_MODES = {
    "",
    "day",
    "week",
    "month",
    "day_male",
    "day_female",
    "week_origin",
    "week_rookie",
    "day_r18",
    "day_female_r18",
    "week_r18",
}

_HEADERS = {
    "User-Agent": "PixivAndroidApp/6.182.0",
    "App-OS": "android",
    "App-OS-Version": "15.0",
    "App-Version": "6.182.0",
    "Accept-Language": "zh-CN",
}


def _client_kwargs(proxy: str) -> dict:
    return {"proxy": proxy} if proxy else {}


async def _get_access_token(proxy: str, refresh_tokens: list[str]) -> str:
    if not refresh_tokens:
        logger.warning("Pixiv 未配置 REFRESH_TOKEN")
        return ""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": random.choice(refresh_tokens),
        "client_id": "MOBrBDS8blbauoSck0ZfDbtuzpyT",
        "client_secret": "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj",
        "include_policy": "true",
    }
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=15.0), **_client_kwargs(proxy)
    ) as client:
        try:
            resp = await client.post(
                "https://oauth.secure.pixiv.net/auth/token",
                headers={"User-Agent": _HEADERS["User-Agent"]},
                data=data,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Pixiv OAuth HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return ""
            token = resp.json().get("access_token") or ""
            if not token:
                logger.warning("Pixiv OAuth 响应没有 access_token")
            return token
        except Exception as exc:  # noqa: BLE001 —— 网络失败按空 token 处理
            logger.warning(
                "Pixiv OAuth 请求异常（检查 PROXY）: %s: %s",
                type(exc).__name__,
                exc,
            )
            return ""


async def _get_rank_info(access_token: str, mode: str, proxy: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {access_token}", **_HEADERS}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=15.0), **_client_kwargs(proxy)
    ) as client:
        try:
            resp = await client.get(
                "https://app-api.pixiv.net/v1/illust/ranking",
                headers=headers,
                params={"mode": mode},
            )
            if resp.status_code != 200:
                logger.warning(
                    "Pixiv ranking mode=%s HTTP %s: %s",
                    mode,
                    resp.status_code,
                    resp.text[:200],
                )
                return []
            return resp.json().get("illusts") or []
        except Exception as exc:  # noqa: BLE001 —— 网络失败按空榜单处理
            logger.warning(
                "Pixiv ranking 请求异常 mode=%s: %s: %s",
                mode,
                type(exc).__name__,
                exc,
            )
            return []


async def _get_image(url: str, proxy: str) -> bytes | None:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=15.0), **_client_kwargs(proxy)
    ) as client:
        try:
            resp = await client.get(
                url,
                headers={
                    "Referer": "https://www.pixiv.net/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                },
            )
        except Exception:  # noqa: BLE001 —— 单张图失败不影响同批其他图
            return None
        return resp.content


def _resource_check(pic_info: dict, num: int = 5) -> list[str]:
    """多图作品超过 num 张时跳过（可能是漫画）。"""
    if pic_info.get("meta_single_page"):
        url = pic_info["meta_single_page"].get("original_image_url")
        return [url] if url else []
    pages = pic_info.get("meta_pages") or []
    if len(pages) > num:
        return []
    return [p["image_urls"]["large"] for p in pages if p.get("image_urls")]


def _pic_check(pic_list: list[str], cache_dir: Path) -> bool:
    """检查是否发过；没发过则记录并返回 False。"""
    sent_path = cache_dir / "sended.json"
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    sent = json.loads(sent_path.read_text()) if sent_path.exists() else []
    ids = [Path(url).stem for url in pic_list]
    if any(i in sent for i in ids):
        return True
    sent.extend(ids)
    sent_path.write_text(json.dumps(sent, ensure_ascii=False))
    return False


async def fetch_pixiv_images(
    mode: str,
    *,
    cache_dir: Path,
    proxy: str = "",
    refresh_tokens: list[str] | None = None,
) -> list[bytes] | str:
    """返回图片字节列表；失败时返回给用户看的错误文本。"""
    # 兼容 day-r18 这类连字符写法；官方 mode 用下划线
    mode = (mode or "").strip().lower().replace("-", "_")
    if mode not in ALLOWED_MODES:
        available = ", ".join(sorted(ALLOWED_MODES - {""}))
        return f"无效参数。可用类型：{available}"
    mode = mode or "day"
    cache_dir = Path(cache_dir)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    info_path = cache_dir / "ranking_info" / f"{day}-{mode}.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)

    if not info_path.exists():
        token = await _get_access_token(proxy, list(refresh_tokens or []))
        if not token:
            return "获取 Pixiv token 失败：检查代理是否可用 / refresh_token 是否失效"
        rank = await _get_rank_info(token, mode, proxy)
        if not rank:
            return "获取排行榜失败，稍后再试"
        info_path.write_text(json.dumps(rank, ensure_ascii=False, indent=2))

    info_list = json.loads(info_path.read_text(encoding="utf-8"))
    pic_list: list[str] = []
    for _ in range(10):
        pic_list = _resource_check(random.choice(info_list))
        if pic_list and _pic_check(pic_list, cache_dir):
            continue
        if pic_list:
            break
    else:
        return "今天的新图都发过了，明天再来吧"

    images: list[bytes] = []
    for url in pic_list:
        data = await _get_image(url, proxy)
        if data:
            images.append(data)
    return images or "图片下载失败，请再试一次"
