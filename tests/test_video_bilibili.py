"""B 站元数据请求：Cookie 透传与 412 风控重试。"""

from __future__ import annotations

import httpx
import pytest

from qq_bot.video.bilibili import BilibiliError, fetch_meta

_META = {
    "code": 0,
    "data": {
        "bvid": "BV1tyno6wEkR",
        "aid": 113000000000,
        "cid": 200000000,
        "title": "主教练正在热身",
        "duration": 123,
        "desc": "desc",
        "owner": {"name": "up"},
    },
}


async def test_fetch_meta_sends_cookie() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, json=_META)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = await fetch_meta(client, bvid="BV1tyno6wEkR", cookie="SESSDATA=abc")

    assert "SESSDATA=abc" in seen["cookie"]
    assert meta.title == "主教练正在热身"
    assert meta.cid == 200000000


async def test_fetch_meta_primes_buvid_on_412() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "api.bilibili.com" in url:
            has_buvid = "buvid3=" in request.headers.get("cookie", "")
            return httpx.Response(200, json=_META) if has_buvid else httpx.Response(412)
        return httpx.Response(
            200,
            headers={"set-cookie": "buvid3=xyz; Domain=.bilibili.com; Path=/"},
            text="ok",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = await fetch_meta(client, bvid="BV1tyno6wEkR")

    assert any("www.bilibili.com/video/BV1tyno6wEkR" in url for url in calls)
    assert sum("api.bilibili.com" in url for url in calls) == 2
    assert meta.aid == 113000000000


async def test_fetch_meta_412_raises_actionable_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(412)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BilibiliError, match="BILIBILI_COOKIE"):
            await fetch_meta(client, bvid="BV1tyno6wEkR")
