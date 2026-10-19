"""外部身份映射：openid → 内部 id。"""

from __future__ import annotations


async def test_identity_is_stable(repo) -> None:
    first = await repo.ensure_identity(
        platform="qq", kind="user", external_id="openid-A", display_name="小葱"
    )
    second = await repo.ensure_identity(
        platform="qq", kind="user", external_id="openid-A"
    )
    assert first == second
    assert first >= 1_000_000_000  # 避开真实 QQ 号段


async def test_identity_kinds_and_platforms_are_separate(repo) -> None:
    user = await repo.ensure_identity(
        platform="qq", kind="user", external_id="same-id"
    )
    group = await repo.ensure_identity(
        platform="qq", kind="group", external_id="same-id"
    )
    onebot_user = await repo.ensure_identity(
        platform="onebot", kind="user", external_id="same-id"
    )
    assert len({user, group, onebot_user}) == 3


async def test_identity_display_name_updates(repo) -> None:
    uid = await repo.ensure_identity(
        platform="qq", kind="user", external_id="openid-B", display_name="旧名"
    )
    await repo.ensure_identity(
        platform="qq", kind="user", external_id="openid-B", display_name="新名"
    )
    name = await repo.pool.fetchval(
        "SELECT display_name FROM identity_map WHERE internal_id = $1", uid
    )
    assert name == "新名"


async def test_identity_of_lookup(repo) -> None:
    uid = await repo.ensure_identity(
        platform="qq", kind="user", external_id="openid-C"
    )
    assert await repo.identity_of(
        platform="qq", kind="user", external_id="openid-C"
    ) == uid
    assert await repo.identity_of(
        platform="qq", kind="user", external_id="missing"
    ) is None
