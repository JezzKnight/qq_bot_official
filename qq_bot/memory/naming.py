"""称呼：归一化、注入转义、设置、解析（设计文档 §8）。

设计要点：
- 称呼是**映射**不是自由文本记忆：生成侧查 `call(uid)`，理解侧查 `resolve(text)`。
- 唯一性交给数据库约束（部分唯一索引），这里只负责把冲突翻译成人话。
- 不做字符合法性校验（用户想叫什么就叫什么），但注入前必须转义（§8.8）。
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from qq_bot.memory.pg_repo import PgRepository

# 注入时的展示上限：只影响注入，不影响存储与匹配
_MAX_INJECT_LEN = 64
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028\u2029]")
# 单字符与短 ASCII 别名容易误伤，解析时要求最小长度
_MIN_RESOLVE_LEN = 2
_SHORT_ASCII_MAX = 3


@dataclass(frozen=True)
class NameResult:
    """set_my_name 的结果：只带原因，不带"建议叫什么"。"""

    ok: bool
    message: str


@dataclass(frozen=True)
class ResolvedMention:
    uid: int
    matched: str


def normalize(name: str) -> str:
    """归一化键：NFKC（全角→半角）→ 折叠空白 → 去首尾 → 小写。"""
    text = unicodedata.normalize("NFKC", name or "")
    return _WS.sub(" ", text).strip().lower()


def escape_for_prompt(name: str) -> str:
    """注入转义：控制字符换空格（防截断提示词结构）→ 限长 → XML 转义。"""
    cleaned = _CONTROL.sub(" ", name or "")
    cleaned = _WS.sub(" ", cleaned).strip()
    return html.escape(cleaned[:_MAX_INJECT_LEN], quote=True)


def _is_short_ascii(key: str) -> bool:
    return (
        len(key) <= _SHORT_ASCII_MAX
        and key.isascii()
        and key.replace(" ", "").isalpha()
    )


def _occurrences(text: str, key: str) -> list[tuple[int, int]]:
    """返回 key 在 text 中的所有区间；短 ASCII 名要求词边界。"""
    if _is_short_ascii(key):
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])")
        return [(m.start(), m.end()) for m in pattern.finditer(text)]
    spans: list[tuple[int, int]] = []
    start = text.find(key)
    while start != -1:
        spans.append((start, start + len(key)))
        start = text.find(key, start + 1)
    return spans


async def set_my_name(
    repo: PgRepository, *, user_id: str, group_id: str | None, name: str
) -> NameResult:
    """设置/清空本人称呼。撞别人的 preferred 或本群他人名片时拒绝（先到先得）。"""
    key = normalize(name)
    if not key:
        await repo.clear_names(user_id)
        return NameResult(ok=True, message="已取消称呼，回落到群名片")

    current = await repo.preferred_name(user_id)
    if current and normalize(current) == key:
        return NameResult(ok=True, message="这就是你现在的称呼")

    owner = await repo.find_preferred_owner(key, user_id)
    if owner is not None:
        who = owner.get("display_name") or str(owner["user_id"])
        return NameResult(ok=False, message=f"「{name.strip()}」已被「{who}」用做称呼")

    if group_id is not None:
        for member in await repo.list_members(group_id):
            if str(member["user_id"]) == user_id:
                continue
            for label in (member.get("card"), member.get("nickname")):
                if label and normalize(label) == key:
                    who = member.get("card") or member.get("nickname")
                    return NameResult(
                        ok=False, message=f"「{name.strip()}」是「{who}」的群名片"
                    )

    try:
        await repo.set_preferred_name(user_id, name.strip(), key)
    except asyncpg.UniqueViolationError:
        return NameResult(ok=False, message=f"「{name.strip()}」已被别人用做称呼")
    return NameResult(ok=True, message=f"已记住，以后叫你「{name.strip()}」")


def _select_uid(
    owners: set[int], preferred: set[int], excluded: set[int]
) -> tuple[int | None, bool]:
    """在同名多人中选一个：唯一则直接取；否则"恰有一个 preferred"取它；再否则判歧义。"""
    uids = owners - excluded
    if not uids:
        return None, False
    if len(uids) == 1:
        return next(iter(uids)), False
    pref = preferred - excluded
    if len(pref) == 1:
        return next(iter(pref)), False
    return None, True


async def resolve_mentions(  # noqa: C901 —— 匹配规则集中在此，拆开反而难读
    repo: PgRepository,
    *,
    group_id: str,
    text: str,
    exclude_uids: set[int] | None = None,
) -> tuple[list[ResolvedMention], list[str]]:
    """解析文本中出现的称呼 → 成员。返回 (命中列表, 歧义别名列表)。

    规则：长度降序匹配、命中区间不重叠；同名多人时若恰有一个 preferred 则取它，
    否则判为歧义（跳过并交给上层记日志）。
    """
    names = await repo.list_names(group_id)
    if not names:
        return [], []

    owners: dict[str, set[int]] = {}
    preferred: dict[str, set[int]] = {}
    for row in names:
        key = row["name_key"]
        if len(key) < _MIN_RESOLVE_LEN:
            continue
        owners.setdefault(key, set()).add(str(row["user_id"]))
        if row["is_preferred"]:
            preferred.setdefault(key, set()).add(str(row["user_id"]))

    text_key = normalize(text)
    consumed = [False] * len(text_key)
    matches: list[ResolvedMention] = []
    ambiguous: list[str] = []
    excluded = exclude_uids or set()

    for key in sorted(owners, key=len, reverse=True):
        spans = [
            (s, e)
            for s, e in _occurrences(text_key, key)
            if not any(consumed[s:e])
        ]
        if not spans:
            continue
        chosen, is_ambiguous = _select_uid(
            owners[key], preferred.get(key, set()), excluded
        )
        if is_ambiguous:
            ambiguous.append(key)
            continue
        if chosen is None:
            continue
        for s, e in spans:
            for i in range(s, e):
                consumed[i] = True
        matches.append(ResolvedMention(uid=chosen, matched=key))
    return matches, ambiguous


async def call_names(
    repo: PgRepository, user_ids: list[str], group_id: str | None
) -> dict[int, str]:
    """给一批 uid 取"该怎么称呼"（weight 最小者：preferred > 别名 > 名片 > 昵称）。"""
    if not user_ids:
        return {}
    wanted = set(user_ids)
    best: dict[int, tuple[int, str]] = {}
    for row in await repo.list_names(group_id):
        uid = str(row["user_id"])
        if uid not in wanted:
            continue
        weight = int(row["weight"])
        if uid not in best or weight < best[uid][0]:
            best[uid] = (weight, row["name"])
    return {uid: value[1] for uid, value in best.items()}
