"""QQ 小程序图片内容安全检测（security.imgSecCheck）。

机器人 AppID 不能直接调用这个接口：实测会返回
``{"errCode": -120070018, "errMsg": "小程序未上线"}``。
因此这里使用独立的、已上线的 QQ 小程序 AppID/Secret 获取 access_token。

接口文档：
https://q.qq.com/wiki/develop/miniprogram/server/open_port/port_safe.html
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

import httpx
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.q.qq.com/api/getToken"
CHECK_URL = "https://api.q.qq.com/api/json/security/ImgSecCheck"
MAX_IMAGE_BYTES = 1_000_000
MAX_WIDTH = 750
MAX_HEIGHT = 1334
PASS_CODE = 0
RISKY_CODE = 87014
TOKEN_INVALID_CODES = {40001, 40014, 42001}
TOKEN_TTL_SECONDS = 7200
TOKEN_REFRESH_MARGIN = 300
CACHE_SIZE = 256


@dataclass(frozen=True)
class ImageCheckResult:
    allowed: bool
    code: int | None = None
    message: str = ""
    detail: str = ""


def _result(
    *,
    allowed: bool,
    code: int | None = None,
    message: str = "",
    detail: str = "",
) -> ImageCheckResult:
    return ImageCheckResult(allowed=allowed, code=code, message=message, detail=detail)


class ImageChecker(Protocol):
    async def check(self, data: bytes) -> ImageCheckResult: ...


def _payload_code(payload: dict) -> int | None:
    for key in ("errcode", "errCode", "code"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _payload_message(payload: dict) -> str:
    for key in ("errmsg", "errMsg", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def prepare_image(data: bytes) -> bytes:
    """把图片压到接口限制内（<=1MB，<=750x1334），不改原图。"""
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.Resampling.LANCZOS)
    except Exception as exc:
        raise ValueError("图片格式不支持") from exc

    for scale in (1.0, 0.9, 0.8, 0.7, 0.6):
        width = max(1, int(image.width * scale))
        height = max(1, int(image.height * scale))
        resized = image.resize((width, height), Image.Resampling.LANCZOS)
        for quality in (88, 80, 72, 64, 56):
            buffer = io.BytesIO()
            resized.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= MAX_IMAGE_BYTES:
                return encoded
    raise ValueError("图片压缩后仍超过 1MB")  # noqa: TRY003


class ImageSecurityChecker:
    """调用 QQ 小程序 ImgSecCheck；任何异常都按“拦截”处理。"""

    def __init__(
        self,
        appid: str,
        secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.appid = appid.strip()
        self.secret = secret.strip()
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._cache: OrderedDict[str, ImageCheckResult] = OrderedDict()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _access_token(self) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at:
                return self._token
            client = await self._http()
            response = await client.get(
                TOKEN_URL,
                params={
                    "grant_type": "client_credential",
                    "appid": self.appid,
                    "secret": self.secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
            token = str(payload.get("access_token") or "")
            if not token:
                raise RuntimeError(
                    _payload_message(payload) or "获取 access_token 失败"
                )
            expires_in = int(payload.get("expires_in") or TOKEN_TTL_SECONDS)
            self._token = token
            self._token_expires_at = now + max(60, expires_in - TOKEN_REFRESH_MARGIN)
            return token

    def _invalidate_token(self) -> None:
        self._token = ""
        self._token_expires_at = 0.0

    def _cache_get(self, digest: str) -> ImageCheckResult | None:
        result = self._cache.get(digest)
        if result is not None:
            self._cache.move_to_end(digest)
        return result

    def _cache_put(self, digest: str, result: ImageCheckResult) -> None:
        self._cache[digest] = result
        self._cache.move_to_end(digest)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    async def check(self, data: bytes) -> ImageCheckResult:  # noqa: PLR0911
        if not self.appid or not self.secret:
            return _result(
                allowed=False,
                message="未配置 QQ 小程序 AppID/Secret",
                detail="QQ_MINI_APPID/QQ_MINI_SECRET 为空",
            )
        try:
            prepared = prepare_image(data)
        except ValueError as exc:
            return _result(allowed=False, message=str(exc))

        digest = hashlib.sha256(prepared).hexdigest()
        cached = self._cache_get(digest)
        if cached is not None:
            return cached

        for attempt in range(2):
            try:
                token = await self._access_token()
                client = await self._http()
                response = await client.post(
                    CHECK_URL,
                    params={"access_token": token},
                    data={"appid": self.appid},
                    files={"media": ("image.jpg", prepared, "image/jpeg")},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("图片安全检测请求失败: %s", exc)
                result = _result(allowed=False, message="图片安全检测服务不可用")
                self._cache_put(digest, result)
                return result

            code = _payload_code(payload)
            message = _payload_message(payload)
            if code == PASS_CODE:
                result = _result(allowed=True, code=code, message=message)
                self._cache_put(digest, result)
                return result
            if code == RISKY_CODE:
                result = _result(
                    allowed=False, code=code, message=message or "图片内容不合规"
                )
                self._cache_put(digest, result)
                return result
            if code in TOKEN_INVALID_CODES and attempt == 0:
                self._invalidate_token()
                continue
            logger.warning("图片安全检测返回异常 code=%s message=%s", code, message)
            result = _result(
                allowed=False,
                code=code,
                message=message or "图片安全检测未通过",
            )
            self._cache_put(digest, result)
            return result

        return _result(allowed=False, message="图片安全检测未通过")


class DisabledImageSecurityChecker:
    """显式关闭审核时的空实现，仅用于运维排障。"""

    async def aclose(self) -> None:
        return None

    async def check(self, data: bytes) -> ImageCheckResult:  # noqa: ARG002
        return _result(allowed=True, message="图片安全审核已关闭")


_checker: ImageChecker | None = None


def _env_enabled() -> bool:
    return os.getenv("IMAGE_SECURITY_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_image_security_checker() -> ImageChecker:
    global _checker  # noqa: PLW0603
    if _checker is None:
        if not _env_enabled():
            _checker = DisabledImageSecurityChecker()
        else:
            _checker = ImageSecurityChecker(
                os.getenv("QQ_MINI_APPID", ""),
                os.getenv("QQ_MINI_SECRET", ""),
            )
    return _checker


def reset_image_security_checker() -> None:
    """测试用：清空单例。"""
    global _checker  # noqa: PLW0603
    _checker = None
