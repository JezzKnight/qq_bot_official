"""身份解析：QQ openid → 内部 id。"""

from __future__ import annotations

from qq_bot.platform.identity import resolve_group, resolve_user


async def test_qq_allocates_high_ids(repo) -> None:
    first = await resolve_user(
        repo, platform="qq", external_id="openid-X", display_name="测试鱼"
    )
    second = await resolve_user(repo, platform="qq", external_id="openid-X")
    assert first == second
    assert first >= 10_000_000_000_000


async def test_qq_group_allocates_high_ids(repo) -> None:
    gid = await resolve_group(repo, platform="qq", external_id="group-openid")
    assert gid >= 10_000_000_000_000
