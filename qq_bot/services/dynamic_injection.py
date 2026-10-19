"""进入模型前的动态长期记忆注入（L3 + 称呼解析）。

与旧实现的区别（设计 §8.6）：
- 全部异步（记忆在 PostgreSQL）；
- **指代解析与记忆注入解耦**：只要解析出成员就注入映射，与该成员有没有记忆无关；
- 记忆选取用「近期 N 条 + 高置信 M 条」，避免高置信旧记忆把新记忆饿死。
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from qq_bot.memory.glossary import format_injection, match_terms
from qq_bot.memory.memory_config import get_memory_config
from qq_bot.memory.naming import escape_for_prompt, resolve_mentions
from qq_bot.memory.selection import format_memory_block, pick_memories
from qq_bot.services.memory_writing import get_memory

if TYPE_CHECKING:
    from qq_bot.memory.pg_repo import PgRepository

logger = logging.getLogger(__name__)


async def _user_memories(repo: PgRepository, uid: int, gid: int | None) -> list[dict]:
    rows = await repo.list_memories(
        user_id=uid,
        group_id=gid,
        fresh_within_days=get_memory_config().retention.memory_stale_days,
    )
    return [
        r for r in rows if r["scope_kind"] == "user" and int(r["scope_id"]) == uid
    ]


async def _glossary_block(
    repo: PgRepository, gid: int, user_input: str, *, enabled: bool
) -> str | None:
    if not enabled:
        return None
    try:
        terms = await repo.list_glossary(gid)
    except Exception:
        logger.warning("术语库匹配失败，跳过术语注入", exc_info=True)
        return None
    hits = match_terms(terms, user_input)
    return format_injection(hits) if hits else None


async def _mention_section(
    repo: PgRepository, gid: int, matches: list
) -> tuple[list[str], list[str]]:
    """被提及成员的记忆块 + 指代解析块。"""
    blocks: list[str] = []
    injected: list[str] = []
    for match in matches:
        rows = await _user_memories(repo, match.uid, gid)
        if rows:
            header = f'## <user id="{match.uid}"/> 的个人记忆'
            blocks.append(format_memory_block(header, pick_memories(rows)))
        injected.append(str(match.uid))
    if matches:
        lines = ["## 指代解析（消息中出现的称呼 → 成员）"]
        lines.extend(f'- 「{m.matched}」→ user id="{m.uid}"' for m in matches)
        blocks.append("\n".join(lines))
    return blocks, injected


async def build_memory_injection(
    group_id: int | str,
    current_user_id: int | str,
    user_input: str,
    self_id: str | None = None,
    *,
    glossary_enabled: bool = True,
) -> tuple[str | None, list[str]]:
    """返回 (注入文本, 命中的成员 uid 列表，含当前发言人本人)。"""
    gid, uid = int(group_id), int(current_user_id)
    repo = await get_memory()
    if repo is None:
        return None, []

    blocks: list[str] = []
    injected: list[str] = []

    group_rows = [
        r
        for r in await repo.list_memories(
            user_id=uid,
            group_id=gid,
            fresh_within_days=get_memory_config().retention.memory_stale_days,
        )
        if r["scope_kind"] == "group" and int(r["scope_id"]) == gid
    ]
    if group_rows:
        blocks.append(format_memory_block("## 群公共记忆", pick_memories(group_rows)))

    self_rows = await _user_memories(repo, uid, gid)
    if self_rows:
        call = await repo.preferred_name(uid) or ""
        header = f'## <user id="{uid}" name="{escape_for_prompt(call)}"/> 的个人记忆'
        blocks.append(format_memory_block(header, pick_memories(self_rows)))
        injected.append(str(uid))

    glossary = await _glossary_block(
        repo, gid, user_input, enabled=glossary_enabled
    )
    if glossary:
        blocks.append(glossary)

    excluded = {uid}
    if self_id:
        with contextlib.suppress(ValueError):
            excluded.add(int(self_id))
    matches, ambiguous = await resolve_mentions(
        repo, group_id=gid, text=user_input, exclude_uids=excluded
    )
    if ambiguous:
        logger.info("称呼歧义，跳过解析: %s", ambiguous)
    mention_blocks, mention_uids = await _mention_section(repo, gid, matches)
    blocks.extend(mention_blocks)
    injected.extend(mention_uids)

    if not blocks:
        return None, injected
    return "\n\n".join(blocks), injected


async def build_private_injection(user_id: int | str) -> str | None:
    """私聊：注入本人长期记忆。"""
    uid = int(user_id)
    repo = await get_memory()
    if repo is None:
        return None
    rows = await _user_memories(repo, uid, None)
    if not rows:
        return None
    return format_memory_block(f'## <user id="{uid}"/> 的个人记忆', pick_memories(rows))
