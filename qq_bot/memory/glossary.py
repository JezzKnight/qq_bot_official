"""群术语库：匹配与注入格式化（纯函数）。

存储已迁到 PostgreSQL 的 ``glossary_terms`` 表（见 qq_bot/memory/pg_repo.py）；
本模块只保留"给定词条与文本，命中哪些、怎么注入"的纯逻辑，便于单测。
"""

from __future__ import annotations

import re

from qq_bot.memory.naming import normalize

# 单次注入的词条上限：防止一条消息命中几十个词把上下文撑爆
MAX_TERMS = 10
# 短 ASCII 词的判定阈值（超过则按普通子串匹配）
_SHORT_ASCII_MAX = 3


def _is_short_ascii(key: str) -> bool:
    """短 ASCII 词（≤3 纯字母）需词边界，避免 'as'/'go' 误伤正常文本。"""
    return (
        len(key) <= _SHORT_ASCII_MAX
        and key.isascii()
        and key.replace(" ", "").isalpha()
    )


def match_terms(terms: list[dict], text: str) -> list[dict]:
    """在 text 中匹配词条，按词条长度降序返回（长词优先，最多 MAX_TERMS 条）。"""
    if not terms or not text:
        return []
    text_key = normalize(text)
    hits: list[dict] = []
    for item in terms:
        key = str(item.get("term_key") or normalize(str(item.get("term", ""))))
        if not key:
            continue
        if _is_short_ascii(key):
            if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text_key):
                hits.append(item)
        elif key in text_key:
            hits.append(item)
    hits.sort(key=lambda t: len(str(t.get("term_key") or "")), reverse=True)
    return hits[:MAX_TERMS]


def format_injection(terms: list[dict]) -> str:
    """生成术语注入块：## 群术语 + - 「term」：definition。"""
    lines = ["## 群术语"]
    lines.extend(
        f"- 「{t.get('term', '')}」：{t.get('definition', '')}" for t in terms
    )
    return "\n".join(lines)
