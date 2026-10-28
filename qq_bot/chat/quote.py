"""短别名引用：模型写 ``<quote m="k3f9"/>``，代码翻译成 QQ 引用。

每轮拼 prompt 时给会话窗口里每条用户消息分配一个随机短别名（4 位、去掉易混
字符），模型只抄这个别名；代码按别名精确查到真实 ``external_id``，不再做文本
相似度匹配，也没有兜底猜测——别名对不上就不引用。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

_TAG_RE = re.compile(r'^\s*<quote\s+m="(?P<alias>[A-Za-z0-9]{2,16})"\s*/>\s*')
_PARAGRAPH_RE = re.compile(r"\n\s*\n")

MAX_QUOTE_CHARS = 200
ALIAS_LENGTH = 4
# 去掉 0/o/1/l/i 这类模型容易抄错的字符
ALIAS_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


@dataclass(frozen=True)
class QuotedReply:
    """解析结果：引用的短别名、回给那人的话、回给当前消息的正文。"""

    alias: str
    quoted_body: str
    rest_body: str


def make_alias(taken: set[str]) -> str:
    """生成一个当前窗口内不重复的随机短别名。"""
    while True:
        alias = "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(ALIAS_LENGTH))
        if alias not in taken:
            return alias


def needs_auto_quote(
    *,
    enabled: bool,
    is_group: bool,
    auto_reply: bool,
    trigger_mid: str,
    latest_mid: str,
) -> bool:
    """发送前发现群里已有更新的消息时，自动引用触发本轮的那条。"""
    return bool(
        enabled
        and is_group
        and auto_reply
        and latest_mid
        and latest_mid != trigger_mid
    )


def parse_quoted_reply(text: str) -> QuotedReply | None:
    """解析开头的 ``<quote m="xxxx"/>``；没有标签或格式不完整时返回 None。"""
    raw = (text or "").strip()
    match = _TAG_RE.match(raw)
    if match is None:
        return None
    alias = match.group("alias").lower()
    remainder = raw[match.end() :].lstrip("\n ")
    if not remainder:
        return None
    parts = _PARAGRAPH_RE.split(remainder, maxsplit=1)
    quoted_body = parts[0].strip()
    rest_body = parts[1].strip() if len(parts) > 1 else ""
    if not quoted_body:
        return None
    if len(quoted_body) > MAX_QUOTE_CHARS:
        quoted_body = quoted_body[:MAX_QUOTE_CHARS].rstrip()
    return QuotedReply(alias=alias, quoted_body=quoted_body, rest_body=rest_body)
