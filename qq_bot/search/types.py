"""搜索链路的纯数据对象：不依赖 NoneBot / 网络，便于单测与基准脚本复用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SearchMode = Literal["fast", "quick", "standard", "deep"]


@dataclass(frozen=True, slots=True)
class SearchItem:
    """一条搜索结果；content 可能是 Tavily 摘要，也可能是 reader 抓取的正文。"""

    title: str
    url: str
    content: str = ""
    score: float = 0.0
    published_date: str = ""
    source: str = "tavily"

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "score": self.score,
            "published_date": self.published_date,
            "source": self.source,
        }


@dataclass(slots=True)
class SearchOutcome:
    """一次搜索的完整结果，供工具层拼上下文、基准脚本记指标。"""

    question: str
    mode: SearchMode
    queries: list[str] = field(default_factory=list)
    items: list[SearchItem] = field(default_factory=list)
    context: str = ""
    reader_sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    cached: bool = False
    search_ms: int = 0
    reader_ms: int = 0
    elapsed_ms: int = 0

    @property
    def source_count(self) -> int:
        return len(self.items)
