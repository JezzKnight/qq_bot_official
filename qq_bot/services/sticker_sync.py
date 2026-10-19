"""表情包库同步：把 stickers 目录 + 清单导入 PG，并缓存注入目录。

文件留在磁盘（不塞 bytea），表里只存 sha256/路径/名称/标签/描述。
启动时同步一次；新增文件后重启即可生效。
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from qq_bot.memory.stickers import StickerMeta, format_catalog, load_manifest

from .memory_writing import get_memory

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO_ROOT / "stickers.yaml"
class _State:
    catalog: str = ""


_state = _State()


def stickers_dir() -> Path:
    """表情包目录。

    优先用 STICKERS_DIR 显式指定：QQ 模式下 ai_chat 插件未被 load_plugin，
    localstore 只能解析到 qqbot_official，会指到错误的目录。
    """
    override = os.getenv("STICKERS_DIR")
    if override:
        return Path(override)
    # 延迟导入：localstore 在模块导入期就要读 NoneBot 配置，测试环境无 driver 会报错
    from nonebot_plugin_localstore import get_plugin_data_dir

    return get_plugin_data_dir() / "stickers"


def catalog_text() -> str:
    """注入给模型的表情包目录（启动同步后生成，空表示不可用）。"""
    return _state.catalog


async def sync_stickers() -> int:
    """扫描目录并幂等导入；返回导入数量。"""
    repo = await get_memory()
    if repo is None:
        return 0
    directory = stickers_dir()
    if not directory.is_dir():
        logger.info("表情包目录不存在，跳过同步: %s", directory)
        return 0

    metas, excluded = load_manifest(_MANIFEST)
    by_file: dict[str, StickerMeta] = {m.file: m for m in metas}
    imported = 0
    paths: list[str] = []

    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in excluded or path.name.startswith("."):
            continue
        meta = by_file.get(path.name) or StickerMeta(
            file=path.name,
            name=path.stem,
            tags=(path.stem,),
            description="",
        )
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            await repo.upsert_sticker(
                sha256=digest,
                path=path.name,
                name=meta.name,
                tags=list(meta.tags),
                description=meta.description,
            )
        except Exception:
            logger.exception("表情包导入失败: %s", path.name)
            continue
        paths.append(path.name)
        imported += 1

    await repo.disable_stickers_except(paths)
    _state.catalog = format_catalog(await repo.list_stickers())
    logger.info("表情包库已同步：%d 个（%s）", imported, directory)
    return imported
