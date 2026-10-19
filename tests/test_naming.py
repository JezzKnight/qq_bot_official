"""称呼：设置 / 撞名 / 旧名滚动 / 解析 / 注入转义。"""

from __future__ import annotations

from qq_bot.memory.naming import (
    call_names,
    escape_for_prompt,
    normalize,
    resolve_mentions,
    set_my_name,
)


async def test_set_and_call(repo) -> None:
    result = await set_my_name(repo, user_id="U7", group_id="G100", name="阿强")
    assert result.ok
    assert await repo.preferred_name("U7") == "阿强"
    assert (await call_names(repo, ["U7"], "G100"))["U7"] == "阿强"


async def test_conflict_with_other_preferred(repo) -> None:
    assert (await set_my_name(repo, user_id="U7", group_id="G100", name="阿强")).ok
    taken = await set_my_name(repo, user_id="U8", group_id="G100", name="阿强")
    assert not taken.ok
    assert "已被" in taken.message
    assert await repo.preferred_name("U8") is None


async def test_conflict_with_group_card(repo) -> None:
    await repo.upsert_members(
        "G100", [{"user_id": "U9", "card": "阿强", "nickname": "q"}]
    )
    result = await set_my_name(repo, user_id="U7", group_id="G100", name="阿强")
    assert not result.ok
    assert "群名片" in result.message


async def test_rename_keeps_single_alias(repo) -> None:
    for name in ("一号", "二号", "三号"):
        assert (await set_my_name(repo, user_id="U7", group_id="G100", name=name)).ok
    rows = [r for r in await repo.list_names("G100") if r["user_id"] == "U7"]
    preferred = [r for r in rows if r["is_preferred"]]
    aliases = [r for r in rows if not r["is_preferred"]]
    assert len(preferred) == 1 and preferred[0]["name"] == "三号"
    assert len(aliases) == 1 and aliases[0]["name"] == "二号"


async def test_clear_removes_all_user_names(repo) -> None:
    await set_my_name(repo, user_id="U7", group_id="G100", name="阿强")
    result = await set_my_name(repo, user_id="U7", group_id="G100", name="")
    assert result.ok
    assert await repo.preferred_name("U7") is None
    assert [r for r in await repo.list_names("G100") if r["user_id"] == "U7"] == []


async def test_same_name_is_noop(repo) -> None:
    await set_my_name(repo, user_id="U7", group_id="G100", name="阿强")
    again = await set_my_name(repo, user_id="U7", group_id="G100", name="阿强")
    assert again.ok
    assert "这就是" in again.message


async def test_resolve_prefers_longest_match(repo) -> None:
    await repo.upsert_members(
        "G100",
        [
            {"user_id": "U7", "card": "小明", "nickname": "x"},
            {"user_id": "U8", "card": "小明明", "nickname": "y"},
        ],
    )
    matches, ambiguous = await resolve_mentions(
        repo, group_id="G100", text="小明明今天来了"
    )
    assert [m.uid for m in matches] == ["U8"]
    assert ambiguous == []


async def test_resolve_ambiguous_name_is_skipped(repo) -> None:
    await repo.upsert_members(
        "G100",
        [
            {"user_id": "U7", "card": "阿强", "nickname": ""},
            {"user_id": "U8", "card": "阿强", "nickname": ""},
        ],
    )
    matches, ambiguous = await resolve_mentions(repo, group_id="G100", text="阿强在哪")
    assert matches == []
    assert ambiguous == ["阿强"]


async def test_resolve_prefers_preferred_after_card_drift(repo) -> None:
    """先设称呼、后有人改名撞上 → 运行期漂移，解析时优先 preferred。"""
    assert (await set_my_name(repo, user_id="U7", group_id="G100", name="阿强")).ok
    await repo.upsert_members(
        "G100", [{"user_id": "U8", "card": "阿强", "nickname": ""}]
    )
    matches, ambiguous = await resolve_mentions(repo, group_id="G100", text="阿强呢")
    assert [m.uid for m in matches] == ["U7"]
    assert ambiguous == []


def test_normalize_fullwidth_and_case() -> None:
    assert normalize("  \uff21\uff22\uff23  ") == "abc"
    assert normalize("阿　强") == "阿 强"


def test_escape_for_prompt_blocks_structure_breaks() -> None:
    assert escape_for_prompt('a"b<c>') == "a&quot;b&lt;c&gt;"
    assert "\n" not in escape_for_prompt("a\nb")
    assert len(escape_for_prompt("字" * 200)) <= 64
