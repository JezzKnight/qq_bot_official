"""token 估算：中文按字、英文按词（沿用旧启发式，避免引入 tokenizer 依赖）。

用途：读取时按预算裁剪上下文、估算群摘要长度（messages 不再存该字段）。
后续若需要更精确，只需替换本模块实现。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(text: str | None) -> int:
    """粗估 token：中文 2/字，英文 1.3/词。"""
    if not text:
        return 0
    chinese = len(_CJK.findall(text))
    non_chinese = _CJK.sub(" ", text)
    english_words = len(non_chinese.split())
    return int(chinese * 2 + english_words * 1.3)
