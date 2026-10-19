"""身份解析：OneBot 自映射与 QQ 序列分配。"""

from __future__ import annotations

import pytest

from qq_bot.platform.identity import resolve_group, resolve_user


async def test_onebot_user_self_maps(repo) -> None:
    uid = await resolve_user(
        repo, platform="onebot", external_id="3215481708", display_name="杨神"
    )
    assert uid == 3215481708
    assert await repo.identity_of(
        platform="onebot", kind="user", external_id="3215481708"
    ) == uid


async def test_onebot_group_self_maps(repo) -> None:
    gid = await resolve_group(repo, platform="onebot", external_id="795245301")
    assert gid == 795245301


async def test_qq_allocates_high_ids(repo) -> None:
    first = await resolve_user(
        repo, platform="qq", external_id="openid-X", display_name="测试鱼"
    )
    second = await resolve_user(repo, platform="qq", external_id="openid-X")
    assert first == second
    assert first >= 10_000_000_000_000


async def test_onebot_requires_numeric_key(repo) -> None:
    with pytest.raises(ValueError):
        await resolve_user(repo, platform="onebot", external_id="not-a-number")
