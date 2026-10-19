"""表情包元数据：清单解析、名称解析、目录格式化（纯函数，便于单测）。

文件本体留在磁盘；本模块不碰 IO（除读取 manifest），也不依赖 NoneBot。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 注入目录时的上限：几十张也够用，超过再考虑检索
CATALOG_LIMIT = 30


@dataclass(frozen=True)
class StickerMeta:
    file: str
    name: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""


class StickerManifestError(Exception):
    """stickers.yaml 格式非法。"""


def load_manifest(path: str | Path) -> tuple[list[StickerMeta], set[str]]:
    """读取清单，返回 (表情列表, 排除文件名集合)。文件缺失时返回空清单。"""
    manifest = Path(path)
    if not manifest.is_file():
        return [], set()
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"stickers.yaml 解析失败: {exc}"
        raise StickerManifestError(msg) from exc
    if not isinstance(data, dict):
        msg = "stickers.yaml 顶层必须是映射（mapping）"
        raise StickerManifestError(msg)

    excluded = {str(x) for x in (data.get("exclude") or [])}
    metas: list[StickerMeta] = []
    for item in data.get("stickers") or []:
        if not isinstance(item, dict) or not item.get("file"):
            msg = "stickers 条目缺少 file"
            raise StickerManifestError(msg)
        metas.append(
            StickerMeta(
                file=str(item["file"]).strip(),
                name=str(item.get("name") or Path(item["file"]).stem).strip(),
                tags=tuple(
                    str(t).strip()
                    for t in (item.get("tags") or [])
                    if str(t).strip()
                ),
                description=str(item.get("description") or "").strip(),
            )
        )
    return metas, excluded


def resolve(rows: list[dict], query: str) -> dict | None:
    """按名称精确 → 标签精确 → 名称包含 的顺序解析；未命中返回 None。"""
    q = (query or "").strip().lower()
    if not q:
        return None
    for row in rows:
        if str(row.get("name", "")).strip().lower() == q:
            return row
    for row in rows:
        if any(str(tag).lower() == q for tag in (row.get("tags") or [])):
            return row
    for row in rows:
        if q in str(row.get("name", "")).lower():
            return row
    return None


def format_catalog(rows: list[dict], *, limit: int = CATALOG_LIMIT) -> str:
    """生成注入给模型的表情包目录。"""
    usable = [r for r in rows if r.get("enabled", True)][:limit]
    if not usable:
        return ""
    lines = [
        "## 表情包",
        "需要发表情时调用 send_sticker(name=...)，"
        "每轮最多一次，会在你这段文字之后发出。",
    ]
    for row in usable:
        tags = "、".join(row.get("tags") or [])
        desc = row.get("description") or ""
        tail = f"（{desc}）" if desc else ""
        tag_text = f" [{tags}]" if tags else ""
        lines.append(f"- {row['name']}{tag_text}{tail}")
    return "\n".join(lines)
