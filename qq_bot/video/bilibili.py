"""B 站元数据与 CC/AI 字幕读取。

字幕接口的事实（2026-10 实测）：
- `x/web-interface/view` 匿名可用，返回 bvid/aid/cid/title/duration；
- 带登录 Cookie 时，`x/player/v2` 可能返回字幕条目，但**无 WBI 签名时
  subtitle_url 可能是错配/随机内容**，不能直接信任；
- `x/player/wbi/v2` 带 WBI 签名（wts + w_rid）时返回的 AI 字幕与视频一致；
- 没有字幕就明确降级到关键帧，不启动 ASR。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import urllib.parse
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
_WBI_MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]
_WBI_FORBIDDEN = "!'()*"


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


async def _prime_buvid(
    client: httpx.AsyncClient, *, bvid: str = "", aid: int | None = None
) -> None:
    """先访问视频页，让 B 站下发 buvid3 等风控 Cookie（匿名 412 时用）。"""
    target = (
        f"https://www.bilibili.com/video/{bvid}/"
        if bvid
        else f"https://www.bilibili.com/video/av{aid}/"
    )
    try:
        await client.get(target, headers=_HEADERS)
    except httpx.HTTPError:
        logger.debug("B 站 Cookie 预热失败: %s", target, exc_info=True)


async def fetch_meta(
    client: httpx.AsyncClient,
    *,
    bvid: str = "",
    aid: int | None = None,
    cookie: str = "",
) -> BilibiliMeta:
    params: dict[str, Any] = {"bvid": bvid} if bvid else {"aid": aid}
    headers = dict(_HEADERS)
    cookie_header = _cookie_header(cookie)
    if cookie_header:
        headers["Cookie"] = cookie_header
    resp = await client.get(
        f"{_API}/x/web-interface/view", params=params, headers=headers
    )
    if resp.status_code == httpx.codes.PRECONDITION_FAILED and not cookie_header:
        # 机房 IP 匿名请求常被 B 站风控为 412：先拿 buvid3 再试一次。
        await _prime_buvid(client, bvid=bvid, aid=aid)
        resp = await client.get(
            f"{_API}/x/web-interface/view", params=params, headers=headers
        )
    if resp.status_code == httpx.codes.PRECONDITION_FAILED:
        msg = (
            "B 站风控拦截（412）：请在 .env 配置 BILIBILI_COOKIE，"
            "或让 B 站请求走非机房出口"
        )
        raise BilibiliError(msg)
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


def _wbi_mixin_key(img_url: str, sub_url: str) -> str:
    """按 B 站 WBI 规则把 img/sub key 混淆成 32 位 mixin key。"""
    img_key = img_url.rsplit("/", 1)[-1].split(".", maxsplit=1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", maxsplit=1)[0]
    raw = img_key + sub_key
    return "".join(raw[i] for i in _WBI_MIXIN_KEY_ENC_TAB)[:32]


def _sign_wbi(params: dict[str, Any], mixin_key: str) -> dict[str, str]:
    """给 WBI 参数加 wts/w_rid；返回可直接传给 httpx 的字符串字典。"""
    filtered = {
        str(key): str(value)
        for key, value in sorted(params.items())
        if not any(ch in str(value) for ch in _WBI_FORBIDDEN)
    }
    query = urllib.parse.urlencode(filtered)
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return filtered


async def _fetch_wbi_mixin_key(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> str:
    resp = await client.get(f"{_API}/x/web-interface/nav", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    img_url = str(wbi.get("img_url") or "")
    sub_url = str(wbi.get("sub_url") or "")
    if not img_url or not sub_url:
        msg = "B 站 WBI key 缺失"
        raise BilibiliError(msg)
    return _wbi_mixin_key(img_url, sub_url)


async def fetch_subtitles(  # noqa: PLR0911
    client: httpx.AsyncClient,
    meta: BilibiliMeta,
    *,
    cookie: str = "",
    max_chars: int = 6_000,
) -> tuple[str, str, list[str]]:
    """返回 (字幕文本, 语言, 警告)。没有字幕时文本为空。"""
    headers = dict(_HEADERS)
    cookie_header = _cookie_header(cookie)
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        mixin_key = await _fetch_wbi_mixin_key(client, headers)
        params = _sign_wbi(
            {
                "bvid": meta.bvid,
                "cid": meta.cid,
                "wts": int(time.time()),
            },
            mixin_key,
        )
        resp = await client.get(
            f"{_API}/x/player/wbi/v2",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError, BilibiliError) as exc:
        msg = f"B 站字幕接口失败: {type(exc).__name__}"
        logger.info("B 站字幕不可用: %s", msg)
        return "", "", [msg]

    if data.get("code") != _JSON_OK:
        msg = f"B 站字幕接口返回错误: {data.get('message') or data.get('code')}"
        logger.info("B 站字幕不可用: %s", msg)
        return "", "", [msg]

    payload = data.get("data") or {}
    subtitles = (payload.get("subtitle") or {}).get("subtitles") or []
    if not subtitles:
        msg = (
            "B 站匿名无法获取 AI 字幕，需配置 BILIBILI_COOKIE"
            if payload.get("need_login_subtitle")
            else "该视频没有可用的 CC 字幕"
        )
        logger.info("B 站字幕不可用: %s", msg)
        return "", "", [msg]

    candidate = next(
        (s for s in subtitles if str(s.get("lan") or "").lower().startswith("zh")),
        subtitles[0],
    )
    url = str(candidate.get("subtitle_url") or "")
    if not url:
        msg = "B 站字幕列表缺少 subtitle_url"
        logger.info("B 站字幕不可用: %s", msg)
        return "", "", [msg]
    if url.startswith("//"):
        url = f"https:{url}"
    try:
        sub_resp = await client.get(url, headers=_HEADERS)
        sub_resp.raise_for_status()
        sub_payload = sub_resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return "", "", [f"B 站字幕下载失败: {type(exc).__name__}"]

    text = parse_subtitle_json(sub_payload, max_chars=max_chars)
    if not text:
        return "", "", ["B 站字幕内容为空"]
    lang = str(candidate.get("lan_doc") or candidate.get("lan") or "字幕")
    logger.info("B 站字幕下载成功: lang=%s chars=%d", lang, len(text))
    return text, lang, []
