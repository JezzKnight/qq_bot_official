"""B 站元数据与 CC/AI 字幕读取。

字幕接口的事实（2026-10 实测）：
- `x/web-interface/view` 匿名可用，返回 bvid/aid/cid/title/duration；
- `x/player/v2` 匿名返回 `subtitle.subtitles=[]`，且 `need_login_subtitle=true`；
  带登录 Cookie 时才可能拿到 AI 字幕（`subtitle_url` 常以 `//aisubtitle...` 开头）；
- UP 主上传的 CC 字幕在部分视频上匿名可见，因此这里不预设“一定没有”，
  而是有就下载、没有就明确降级，不启动 ASR。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .types import BilibiliMeta

logger = logging.getLogger(__name__)

_API = "https://api.bilibili.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://www.bilibili.com/",
}
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"/video/av(\d+)", re.IGNORECASE)
_JSON_OK = 0


class BilibiliError(RuntimeError):
    """B 站接口返回错误。"""


def _cookie_header(cookie: str) -> str:
    cookie = (cookie or "").strip()
    if not cookie:
        return ""
    return cookie if "=" in cookie else f"SESSDATA={cookie}"


async def resolve_b23(client: httpx.AsyncClient, url: str) -> str:
    """把 b23.tv 短链解析成真实视频页 URL；失败原样返回。"""
    try:
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
        return str(resp.url)
    except httpx.HTTPError:
        return url


async def fetch_meta(
    client: httpx.AsyncClient,
    *,
    bvid: str = "",
    aid: int | None = None,
) -> BilibiliMeta:
    params: dict[str, Any] = {"bvid": bvid} if bvid else {"aid": aid}
    resp = await client.get(
        f"{_API}/x/web-interface/view", params=params, headers=_HEADERS
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != _JSON_OK:
        msg = f"B 站视频信息接口失败: {data.get('message') or data.get('code')}"
        raise BilibiliError(msg)
    raw = data.get("data") or {}
    pages = raw.get("pages") or []
    cid = raw.get("cid") or (pages[0].get("cid") if pages else 0)
    if not cid:
        msg = "B 站视频缺少 cid，无法读取字幕"
        raise BilibiliError(msg)
    owner = raw.get("owner") or {}
    return BilibiliMeta(
        bvid=str(raw.get("bvid") or bvid),
        aid=int(raw.get("aid") or aid or 0),
        cid=int(cid),
        title=str(raw.get("title") or ""),
        duration=int(raw.get("duration") or 0),
        description=str(raw.get("desc") or ""),
        owner=str(owner.get("name") or ""),
    )


def parse_subtitle_json(payload: dict[str, Any], *, max_chars: int) -> str:
    """把 B 站字幕 JSON 的 body 数组转成带时间戳的纯文本。"""
    lines: list[str] = []
    total = 0
    for item in payload.get("body") or []:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        try:
            start = float(item.get("from") or 0)
        except (TypeError, ValueError):
            start = 0.0
        minute, second = divmod(int(start), 60)
        line = f"[{minute:02d}:{second:02d}] {content}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


async def fetch_subtitles(  # noqa: PLR0911 —— 字幕有多种降级路径
    client: httpx.AsyncClient,
    meta: BilibiliMeta,
    *,
    cookie: str = "",
    max_chars: int = 12_000,
) -> tuple[str, str, list[str]]:
    """返回 (字幕文本, 语言, 警告)。没有字幕时文本为空。"""
    headers = dict(_HEADERS)
    cookie_header = _cookie_header(cookie)
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        resp = await client.get(
            f"{_API}/x/player/v2",
            params={"bvid": meta.bvid, "cid": meta.cid},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return "", "", [f"B 站字幕接口失败: {type(exc).__name__}"]

    if data.get("code") != _JSON_OK:
        return (
            "",
            "",
            [f"B 站字幕接口返回错误: {data.get('message') or data.get('code')}"],
        )

    payload = data.get("data") or {}
    subtitles = (payload.get("subtitle") or {}).get("subtitles") or []
    if not subtitles:
        if payload.get("need_login_subtitle"):
            return "", "", ["B 站匿名无法获取 AI 字幕，需配置 BILIBILI_COOKIE"]
        return "", "", ["该视频没有可用的 CC 字幕"]

    # 优先中文人工/AI 字幕，其次任意可下载字幕。
    chosen = next(
        (s for s in subtitles if str(s.get("lan") or "").lower().startswith("zh")),
        subtitles[0],
    )
    url = str(chosen.get("subtitle_url") or "")
    if not url:
        return "", "", ["B 站字幕列表缺少 subtitle_url"]
    if url.startswith("//"):
        url = f"https:{url}"
    try:
        sub_resp = await client.get(url, headers=_HEADERS)
        sub_resp.raise_for_status()
        payload = sub_resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return "", "", [f"B 站字幕下载失败: {type(exc).__name__}"]

    text = parse_subtitle_json(payload, max_chars=max_chars)
    if not text:
        return "", "", ["B 站字幕内容为空"]
    lang = str(chosen.get("lan_doc") or chosen.get("lan") or "字幕")
    return text, lang, []
