"""表情包：清单解析、名称解析、目录注入、仓库读写。"""

from __future__ import annotations

from qq_bot.memory.stickers import format_catalog, load_manifest, resolve

MANIFEST = """
exclude:
  - online.jpg
stickers:
  - file: wuyu.jpg
    name: 无语
    tags: [无语, 无奈]
    description: 表达无语时用
  - file: dance.gif
    name: 跳舞
    tags: [跳舞]
    description: 被要求跳舞时用
"""


def test_load_manifest(tmp_path) -> None:
    path = tmp_path / "stickers.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    metas, excluded = load_manifest(path)
    assert excluded == {"online.jpg"}
    assert [m.name for m in metas] == ["无语", "跳舞"]
    assert metas[0].tags == ("无语", "无奈")


def test_manifest_missing_returns_empty(tmp_path) -> None:
    metas, excluded = load_manifest(tmp_path / "nope.yaml")
    assert metas == [] and excluded == set()


def test_resolve_by_name_tag_and_contains() -> None:
    rows = [
        {"name": "无语", "tags": ["无语", "无奈"], "description": "d", "enabled": True},
        {"name": "跳舞", "tags": ["跳舞"], "description": "d", "enabled": True},
    ]
    assert resolve(rows, "无语")["name"] == "无语"
    assert resolve(rows, "无奈")["name"] == "无语"
    assert resolve(rows, "跳")["name"] == "跳舞"
    assert resolve(rows, "不存在") is None


def test_format_catalog_contains_usage_hint() -> None:
    rows = [
        {
            "name": "无语",
            "tags": ["无语", "无奈"],
            "description": "表达无语时用",
            "enabled": True,
        }
    ]
    text = format_catalog(rows)
    assert "## 表情包" in text
    assert "send_sticker" in text
    assert "- 无语 [无语、无奈]（表达无语时用）" in text
    assert format_catalog([]) == ""


async def test_repo_upsert_and_usage(repo) -> None:
    first = await repo.upsert_sticker(
        sha256="a" * 64, path="wuyu.jpg", name="无语", tags=["无语"], description="d"
    )
    assert first == "created"
    again = await repo.upsert_sticker(
        sha256="a" * 64,
        path="wuyu.jpg",
        name="无语",
        tags=["无语", "无奈"],
        description="d2",
    )
    assert again == "updated"
    rows = await repo.list_stickers()
    assert len(rows) == 1
    assert rows[0]["tags"] == ["无语", "无奈"]

    found = await repo.get_sticker_by_name("无语")
    assert found is not None
    await repo.bump_sticker_usage(int(found["id"]))
    assert (await repo.list_stickers())[0]["usage_count"] == 1


async def test_repo_disable_missing(repo) -> None:
    await repo.upsert_sticker(
        sha256="b" * 64, path="gone.jpg", name="旧图", tags=[], description=""
    )
    disabled = await repo.disable_stickers_except(["other.jpg"])
    assert disabled == 1
    assert await repo.list_stickers() == []
