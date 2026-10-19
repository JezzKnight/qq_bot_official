"""外部身份解析：平台原始标识 → 内部 BIGINT id。

OneBot 用自映射（internal_id = QQ 号），因此现有数据零迁移；
QQ 官方用高位序列分配（见 v007/v008），与 QQ 号段隔离。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_bot.memory.pg_repo import PgRepository
    from qq_bot.platform.ports import Platform


def _onebot_id(external_id: str) -> int:
    try:
        return int(external_id)
    except ValueError as exc:
        raise ValueError(  # noqa: TRY003 —— 带上原始值便于排查
            f"OneBot 身份必须是数字 QQ 号: {external_id!r}"
        ) from exc


async def resolve_user(
    repo: PgRepository,
    *,
    platform: Platform,
    external_id: str,
    display_name: str | None = None,
) -> int:
    """取或分配用户内部 id。"""
    if platform == "onebot":
        internal = _onebot_id(external_id)
        await repo.bind_identity(
            platform=platform,
            kind="user",
            external_id=external_id,
            internal_id=internal,
            display_name=display_name,
        )
        return internal
    return await repo.ensure_identity(
        platform=platform,
        kind="user",
        external_id=external_id,
        display_name=display_name,
    )


async def resolve_group(
    repo: PgRepository,
    *,
    platform: Platform,
    external_id: str,
) -> int:
    """取或分配群内部 id。"""
    if platform == "onebot":
        internal = _onebot_id(external_id)
        await repo.bind_identity(
            platform=platform,
            kind="group",
            external_id=external_id,
            internal_id=internal,
        )
        return internal
    return await repo.ensure_identity(
        platform=platform, kind="group", external_id=external_id
    )
