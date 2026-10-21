"""结果清洗、去重、轻量相关性重排与上下文拼装。

这里不调用额外模型：Tavily 的原始顺序 + 词项覆盖 + 域名多样性，成本低且可解释。
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from qq_bot.search.types import SearchItem

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9.+#_-]*")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RE = re.compile(
    r"(?is)<(?:script|style|noscript)\b.*?</(?:script|style|noscript)>"
)
_SPACE_RE = re.compile(r"[ \t\f\v]+")
_MIN_DATED_ITEMS = 2
_BLANK_RE = re.compile(r"\n{3,}")

_MAX_CONTEXT_CHARS = 7000
_MAX_ITEM_CHARS = 1600
_MAX_PER_DOMAIN = 2
_MAX_CJK_RUN = 6
_MIN_TAIL_CHARS = 200


def clean_snippet(raw: str, *, max_chars: int = _MAX_ITEM_CHARS) -> str:
    """清洗 Tavily 摘要 / 网页正文，保留中文句子，去掉标签与多余空白。"""
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = _BLANK_RE.sub("\n\n", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        # 尽量在句末截断，避免半句话
        boundary = max(
            cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("\n")
        )
        if boundary > max_chars // 2:
            cut = cut[: boundary + 1]
        text = cut.rstrip() + "…"
    return text


def _terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = set(_WORD_RE.findall(lowered))
    for run in _CJK_RE.findall(lowered):
        if len(run) <= _MAX_CJK_RUN:
            terms.add(run)
        for size in (2, 3, 4):
            if len(run) >= size:
                terms.update(run[i : i + size] for i in range(len(run) - size + 1))
    return terms


def _overlap_ratio(query_terms: set[str], text: str) -> float:
    if not query_terms:
        return 0.0
    haystack = _terms(text)
    if not haystack:
        return 0.0
    return len(query_terms & haystack) / len(query_terms)


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _recency_bonus(published_date: str) -> float:
    """越新的来源加分越多；Tavily 可能返回 RFC 2822 或 ISO 日期。"""
    timestamp = published_timestamp(published_date)
    if timestamp <= 0:
        return 0.0
    published = datetime.fromtimestamp(timestamp, tz=ZoneInfo("Asia/Shanghai")).date()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    age_days = max(0, (today - published).days)
    return 0.12 * max(0.0, 1.0 - age_days / 180)


def published_timestamp(published_date: str) -> float:
    """解析 Tavily 的 RFC 2822 日期或 ISO 日期，供“按新到旧”排序。"""
    if not published_date:
        return 0.0
    parsed = None
    try:
        parsed = parsedate_to_datetime(published_date)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(published_date.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.timestamp()


def sort_by_recency(items: list[SearchItem]) -> list[SearchItem]:
    """时效问题按发布时间从新到旧；无日期的条目沉底，不丢结果。"""
    dated = sum(1 for item in items if item.published_date)
    if dated < _MIN_DATED_ITEMS:
        return items
    return sorted(
        items,
        key=lambda item: published_timestamp(item.published_date),
        reverse=True,
    )


def dedupe_and_rerank(  # noqa: C901 - 去重 + 打分 + 分轮域名配额，保持单函数便于解释
    items: list[SearchItem],
    query: str,
    *,
    limit: int,
) -> list[SearchItem]:
    """URL/标题去重 + 词项覆盖打分 + 单域名上限，避免一个站点刷屏。"""
    query_terms = _terms(query)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    scored: list[tuple[float, int, SearchItem]] = []

    for rank, item in enumerate(items):
        url_key = normalize_url(item.url)
        title_key = item.title.strip().casefold()
        if (
            not url_key
            or url_key in seen_urls
            or (title_key and title_key in seen_titles)
        ):
            continue
        seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)

        title_ratio = _overlap_ratio(query_terms, item.title)
        content_ratio = _overlap_ratio(query_terms, item.content[:2000])
        rank_score = 1.0 / (rank + 2)
        score = 0.45 * title_ratio + 0.35 * content_ratio + 0.15 * rank_score
        if item.published_date:
            score += 0.05 + _recency_bonus(item.published_date)
        scored.append((score, rank, item))

    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    candidates = [entry[2] for entry in scored]

    selected: list[SearchItem] = []
    used_urls: set[str] = set()
    per_domain: dict[str, int] = {}
    # 三轮：先保证每个域名一条，再允许第二、第三条，最后才放开上限。
    # 这样 limit 很小时也不会被同一个站点包场。
    for domain_cap in (1, _MAX_PER_DOMAIN, 10**9):
        for item in candidates:
            if limit > 0 and len(selected) >= limit:
                break
            url_key = normalize_url(item.url)
            if url_key in used_urls:
                continue
            domain = _domain(item.url)
            if per_domain.get(domain, 0) >= domain_cap:
                continue
            selected.append(item)
            used_urls.add(url_key)
            per_domain[domain] = per_domain.get(domain, 0) + 1
        if limit > 0 and len(selected) >= limit:
            break

    return selected[:limit]


def build_context(
    items: list[SearchItem], *, max_chars: int = _MAX_CONTEXT_CHARS
) -> str:
    """拼成带 [S1] 编号的上下文；编号供最终回答引用。"""
    if not items:
        return ""
    blocks: list[str] = []
    used = 0
    for index, item in enumerate(items, start=1):
        snippet = clean_snippet(item.content)
        header = f"[S{index}] {item.title.strip() or item.url}\nURL: {item.url}"
        if item.published_date:
            header += f"\n日期: {item.published_date}"
        block = f"{header}\n{snippet}" if snippet else header
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > _MIN_TAIL_CHARS:
                blocks.append(block[:remaining].rstrip() + "…")
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)
