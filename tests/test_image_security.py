"""QQ 小程序图片安全检测：压缩、调用、缓存与失败拦截。"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from PIL import Image

from qq_bot.platform.qqbot.image_security import (
    MAX_HEIGHT,
    MAX_IMAGE_BYTES,
    MAX_WIDTH,
    ImageSecurityChecker,
    prepare_image,
)


def _png(width: int = 64, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeApi:
    def __init__(self, check_payload: dict[str, Any] | None = None) -> None:
        self.check_payload = check_payload or {"errcode": 0, "errmsg": "ok"}
        self.token_calls = 0
        self.check_calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getToken"):
            self.token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "test-token", "expires_in": 7200},
            )
        if request.url.path.endswith("/ImgSecCheck"):
            self.check_calls += 1
            return httpx.Response(200, json=self.check_payload)
        return httpx.Response(404)


def _checker(api: FakeApi) -> ImageSecurityChecker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return ImageSecurityChecker("mini-appid", "mini-secret", client=client)


async def test_safe_image_passes_and_token_is_cached() -> None:
    api = FakeApi()
    checker = _checker(api)
    try:
        first = await checker.check(_png())
        second = await checker.check(_png(65, 65))
    finally:
        await checker.aclose()

    assert first.allowed is True
    assert second.allowed is True
    assert api.token_calls == 1
    assert api.check_calls == 2


async def test_risky_image_is_blocked() -> None:
    api = FakeApi({"errcode": 87014, "errmsg": "risky"})
    checker = _checker(api)
    try:
        result = await checker.check(_png())
    finally:
        await checker.aclose()

    assert result.allowed is False
    assert result.code == 87014
    assert "risky" in result.message


async def test_missing_credentials_fail_closed() -> None:
    checker = ImageSecurityChecker("", "")
    result = await checker.check(_png())
    assert result.allowed is False
    assert "未配置" in result.message


async def test_prepare_image_respects_limits() -> None:
    source = Image.effect_noise((1200, 2400), 100).convert("RGB")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    prepared = prepare_image(buffer.getvalue())

    assert len(prepared) <= MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.width <= MAX_WIDTH
        assert image.height <= MAX_HEIGHT


async def test_invalid_image_is_blocked() -> None:
    checker = ImageSecurityChecker("mini-appid", "mini-secret")
    result = await checker.check(b"not-an-image")
    assert result.allowed is False
    assert "图片格式" in result.message
    await checker.aclose()


@pytest.mark.parametrize("payload", [{"errCode": 87014}, {"code": 87014}])
async def test_code_field_aliases_are_supported(payload: dict[str, Any]) -> None:
    api = FakeApi(payload)
    checker = _checker(api)
    try:
        result = await checker.check(_png())
    finally:
        await checker.aclose()
    assert result.allowed is False
    assert result.code == 87014
