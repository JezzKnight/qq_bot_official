"""指令文本解析（平台无关）。"""

from __future__ import annotations


def parse_command(text: str) -> tuple[str, str]:
    """解析 `/name args`，返回 (小写指令名, 剩余参数)；不是指令时返回 ("", "")。"""
    body = (text or "").strip()
    if not body.startswith("/"):
        return "", ""
    body = body[1:].strip()
    if not body:
        return "", ""
    parts = body.split(maxsplit=1)
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""
