"""PostgreSQL 记忆仓库（asyncpg）。

约定：
- SQL 全部参数化（``$1..$n``）；
- jsonb 列直接传 Python ``dict`` / ``list``（连接上注册了 codec）；
- 时间列统一 ``timestamptz``，Python 侧只用 tz-aware ``datetime``；
- 私聊线程的 ``group_id`` 为 NULL，靠 ``NULLS NOT DISTINCT`` 唯一约束保证幂等。
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from qq_bot.ai.types import ChatMessage

from .token_est import estimate_tokens


async def _init_conn(conn: asyncpg.Connection) -> None:
    """为每个连接注册 jsonb codec，使 dict/list 可直传直取。"""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class PgRepository:
    """记忆存储的 PostgreSQL 实现。"""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(
        cls, dsn: str, *, min_size: int = 1, max_size: int = 8
    ) -> PgRepository:
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size, init=_init_conn
        )
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    # ── 线程 ──────────────────────────────────────────────

    async def ensure_thread(self, kind: str, group_id: str | None, user_id: str) -> int:
        """取或建线程，并刷新活跃时间。"""
        row = await self.pool.fetchrow(
            """
            INSERT INTO threads (kind, group_id, user_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (kind, group_id, user_id)
            DO UPDATE SET last_active_at = now()
            RETURNING id
            """,
            kind,
            group_id,
            user_id,
        )
        return int(row["id"])

    # ── 消息 ──────────────────────────────────────────────

    async def append(  # noqa: PLR0913 —— 一行的完整列，拆开会更难读
        self,
        *,
        thread_id: int,
        group_id: str | None,
        sender_id: str | None,
        role: str,
        content: str | None,
        sender_name: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        images: list[dict[str, Any]] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        llm_calls: int = 0,
        model: str | None = None,
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO messages
                (thread_id, group_id, sender_id, sender_name, role, content,
                 tool_calls, tool_call_id, images,
                 prompt_tokens, completion_tokens, cached_tokens, llm_calls, model)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14)
            RETURNING id
            """,
            thread_id,
            group_id,
            sender_id,
            sender_name,
            role,
            content,
            tool_calls,
            tool_call_id,
            images,
            prompt_tokens,
            completion_tokens,
            cached_tokens,
            llm_calls,
            model,
        )
        return int(row["id"])

    async def history(self, thread_id: int, limit: int) -> list[ChatMessage]:
        """个人线程最近 limit 条，按时间升序返回。"""
        rows = await self.pool.fetch(
            """
            SELECT role, sender_name, content, tool_call_id, tool_calls
              FROM messages WHERE thread_id = $1 ORDER BY id DESC LIMIT $2
            """,
            thread_id,
            limit,
        )
        return [self._to_message(r) for r in reversed(rows)]

    async def group_window(
        self, group_id: str, exclude_thread_id: int, limit: int
    ) -> list[ChatMessage]:
        """群窗口：他人的最近 limit 条（排除本人线程，保证与个人线程不重叠）。"""
        rows = await self.pool.fetch(
            """
            SELECT role, sender_name, content, tool_call_id, tool_calls
              FROM messages
             WHERE group_id = $1 AND thread_id <> $2
             ORDER BY id DESC LIMIT $3
            """,
            group_id,
            exclude_thread_id,
            limit,
        )
        return [self._to_message(r) for r in reversed(rows)]

    async def clear_thread(self, thread_id: int) -> int:
        """清空线程消息，返回删除行数。"""
        status = await self.pool.execute(
            "DELETE FROM messages WHERE thread_id = $1", thread_id
        )
        return int(status.split()[-1])

    async def messages_by_date(
        self,
        *,
        date_str: str,
        limit: int,
        group_id: str | None = None,
        thread_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """按日期查消息（按 Asia/Shanghai 自然日），返回扁平 dict。"""
        if group_id is None and thread_id is None:
            return []
        rows = await self.pool.fetch(
            """
            SELECT role, sender_name, content, created_at
              FROM messages
             WHERE ($1::text IS NULL OR group_id = $1)
               AND ($2::bigint IS NULL OR thread_id = $2)
               AND (created_at AT TIME ZONE 'Asia/Shanghai')::date = $3::text::date
             ORDER BY id ASC LIMIT $4
            """,
            group_id,
            thread_id,
            date_str,
            limit,
        )
        return [
            {
                "role": r["role"],
                "sender_name": r["sender_name"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    # ── 摘要（P2 使用）────────────────────────────────────

    async def latest_covers_to(self, group_id: str) -> int:
        value = await self.pool.fetchval(
            "SELECT max(covers_to) FROM group_summaries WHERE group_id = $1", group_id
        )
        return int(value or 0)

    async def clear_group_summaries(self, group_id: str) -> int:
        """清空某群摘要，返回删除条数（管理员 /reset group 用）。"""
        result = await self.pool.execute(
            "DELETE FROM group_summaries WHERE group_id = $1", group_id
        )
        return int(result.split()[-1])

    async def clear_group_threads(self, group_id: str) -> tuple[int, int]:
        """删除本群所有成员线程（消息随线程级联删除），返回 (线程数, 消息数)。"""
        row = await self.pool.fetchrow(
            """
            SELECT count(*) AS threads,
                   (SELECT count(*) FROM messages WHERE group_id = $1) AS messages
              FROM threads
             WHERE kind = 'group_member' AND group_id = $1
            """,
            group_id,
        )
        await self.pool.execute(
            "DELETE FROM threads WHERE kind = 'group_member' AND group_id = $1",
            group_id,
        )
        return int(row["threads"]), int(row["messages"])

    async def clear_group_memories(self, group_id: str) -> int:
        """清空本群公共记忆（scope_kind='group'），返回删除条数。"""
        result = await self.pool.execute(
            "DELETE FROM memories WHERE scope_kind = 'group' AND scope_id = $1",
            group_id,
        )
        return int(result.split()[-1])

    async def latest_summary(self, group_id: str) -> str | None:
        return await self.pool.fetchval(
            """
            SELECT content FROM group_summaries
             WHERE group_id = $1 ORDER BY covers_to DESC LIMIT 1
            """,
            group_id,
        )

    async def count_user_messages_since(self, group_id: str, after_id: int) -> int:
        value = await self.pool.fetchval(
            """
            SELECT count(*) FROM messages
             WHERE group_id = $1 AND id > $2 AND role = 'user'
            """,
            group_id,
            after_id,
        )
        return int(value or 0)

    async def cutoff_for_window(self, group_id: str, window: int) -> int:
        """群窗口最旧一条之前的 id（即压缩上界）。窗口不足时返回 0。"""
        value = await self.pool.fetchval(
            """
            SELECT min(id) FROM (
                SELECT id FROM messages WHERE group_id = $1 ORDER BY id DESC LIMIT $2
            ) AS window_ids
            """,
            group_id,
            window,
        )
        return int(value or 0)

    async def group_messages_between(
        self, group_id: str, after_id: int, until_id: int
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, sender_name, role, content
              FROM messages
             WHERE group_id = $1 AND id > $2 AND id < $3
             ORDER BY id ASC
            """,
            group_id,
            after_id,
            until_id,
        )
        return [dict(r) for r in rows]

    async def count_summaries(self, group_id: str) -> int:
        value = await self.pool.fetchval(
            "SELECT count(*) FROM group_summaries WHERE group_id = $1", group_id
        )
        return int(value or 0)

    async def group_messages_for_rebuild(
        self, group_id: str, until_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """全量重建用：取 cutoff 之前最近 limit 条，按时间正序返回。"""
        rows = await self.pool.fetch(
            """
            SELECT id, sender_name, role, content
              FROM messages
             WHERE group_id = $1 AND id < $2
             ORDER BY id DESC LIMIT $3
            """,
            group_id,
            until_id,
            limit,
        )
        return [dict(r) for r in reversed(rows)]

    async def active_group_ids(self, days: int = 7) -> list[str]:
        """最近活跃的群（启动补扫用）。"""
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT group_id FROM threads
             WHERE group_id IS NOT NULL
               AND last_active_at > now() - make_interval(days => $1)
            """,
            days,
        )
        return [str(r["group_id"]) for r in rows]

    async def insert_summary(
        self, *, group_id: str, content: str, covers_from: int, covers_to: int
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO group_summaries
                (group_id, content, covers_from, covers_to, token_est)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (group_id, covers_to) DO NOTHING
            """,
            group_id,
            content,
            covers_from,
            covers_to,
            estimate_tokens(content),
        )

    # ── 身份 ──────────────────────────────────────────────

    async def upsert_members(
        self, group_id: str, members: list[dict[str, Any]]
    ) -> None:
        """批量写入群成员（users + group_members + 名片别名）。"""
        if not members:
            return
        async with self.pool.acquire() as conn, conn.transaction():
            for m in members:
                uid = str(m["user_id"])
                card = (m.get("card") or "").strip()
                nickname = (m.get("nickname") or "").strip()
                display = card or nickname
                await conn.execute(
                    """
                    INSERT INTO users (user_id, display_name)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id)
                    DO UPDATE SET display_name = EXCLUDED.display_name,
                                  last_seen = now()
                    """,
                    uid,
                    display,
                )
                await conn.execute(
                    """
                    INSERT INTO group_members (group_id, user_id, card, nickname)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (group_id, user_id)
                    DO UPDATE SET card = EXCLUDED.card,
                                  nickname = EXCLUDED.nickname,
                                  updated_at = now()
                    """,
                    group_id,
                    uid,
                    card,
                    nickname,
                )
                # 名片/昵称同步进 user_names，供"消息里提到某人"的解析使用；
                # 改名片时先清旧行，避免历史名片被当成有效别名
                from .naming import normalize

                await conn.execute(
                    "DELETE FROM user_names WHERE group_id = $1 AND user_id = $2 "
                    "  AND source IN ('card', 'nickname')",
                    group_id,
                    uid,
                )
                for source, weight, value in (
                    ("card", 100, card),
                    ("nickname", 110, nickname),
                ):
                    if not value:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO user_names
                            (user_id, group_id, name, name_key,
                             is_preferred, source, weight)
                        VALUES ($1, $2, $3, $4, FALSE, $5, $6)
                        ON CONFLICT (user_id, group_id, name_key) DO NOTHING
                        """,
                        uid,
                        group_id,
                        value,
                        normalize(value),
                        source,
                        weight,
                    )

    async def list_members(self, group_id: str) -> list[dict[str, Any]]:
        """返回群成员及其称呼（preferred 优先，其次名片/昵称）。"""
        rows = await self.pool.fetch(
            """
            SELECT gm.user_id,
                   gm.card,
                   gm.nickname,
                   (SELECT un.name FROM user_names un
                     WHERE un.user_id = gm.user_id AND un.is_preferred
                     ORDER BY un.weight LIMIT 1) AS call
              FROM group_members gm
             WHERE gm.group_id = $1
             ORDER BY gm.user_id
            """,
            group_id,
        )
        return [dict(r) for r in rows]

    # ── 称呼 ─────────────────────────────────────────────

    async def list_names(self, group_id: str | None) -> list[dict[str, Any]]:
        """本群 + 全局的所有称呼行（preferred / 别名 / 名片）。"""
        rows = await self.pool.fetch(
            """
            SELECT id, user_id, name, name_key, is_preferred, weight
              FROM user_names
             WHERE group_id IS NULL OR group_id = $1
             ORDER BY weight ASC, length(name) DESC
            """,
            group_id,
        )
        return [dict(r) for r in rows]

    async def find_preferred_owner(
        self, name_key: str, exclude_uid: str
    ) -> dict[str, Any] | None:
        """该名字是否已被别人设为 preferred（先到先得）。"""
        row = await self.pool.fetchrow(
            """
            SELECT n.user_id, coalesce(u.display_name, '') AS display_name
              FROM user_names n
              LEFT JOIN users u ON u.user_id = n.user_id
             WHERE n.name_key = $1 AND n.is_preferred AND n.user_id <> $2
             LIMIT 1
            """,
            name_key,
            exclude_uid,
        )
        return dict(row) if row else None

    async def set_preferred_name(self, user_id: str, name: str, name_key: str) -> None:
        """改名：删旧别名 → 当前 preferred 降级为别名 → 写入新 preferred。

        任何时刻每位用户最多 1 个 preferred + 1 个旧别名（由部分唯一索引保证）。
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM user_names "
                " WHERE user_id = $1 AND group_id IS NULL AND NOT is_preferred",
                user_id,
            )
            await conn.execute(
                "UPDATE user_names SET is_preferred = FALSE, weight = 50, "
                "       updated_at = now() "
                " WHERE user_id = $1 AND group_id IS NULL AND is_preferred",
                user_id,
            )
            await conn.execute(
                "INSERT INTO user_names "
                "  (user_id, group_id, name, name_key, is_preferred, source, weight) "
                "VALUES ($1, NULL, $2, $3, TRUE, 'user', 10)",
                user_id,
                name,
                name_key,
            )

    async def clear_names(self, user_id: str) -> None:
        """清空本人设定的称呼（名片同步行保留）。"""
        await self.pool.execute(
            "DELETE FROM user_names WHERE user_id = $1 AND source = 'user'", user_id
        )

    async def preferred_name(self, user_id: str) -> str | None:
        return await self.pool.fetchval(
            "SELECT name FROM user_names WHERE user_id = $1 AND is_preferred "
            " ORDER BY weight LIMIT 1",
            user_id,
        )

    # ── 长期记忆（L3）────────────────────────────────────

    async def upsert_memory(  # noqa: PLR0913 —— 一条记忆的完整字段
        self,
        *,
        scope_kind: str,
        scope_id: str,
        key: str,
        content: str,
        mem_type: str,
        confidence: float = 0.7,
        source_message_id: int | None = None,
    ) -> str:
        """新增/更新/删除一条长期记忆，返回 created / updated / deleted / missing。"""
        async with self.pool.acquire() as conn, conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM memories "
                " WHERE scope_kind = $1 AND scope_id = $2 AND key = $3",
                scope_kind,
                scope_id,
                key,
            )
            if not content.strip():
                if not exists:
                    return "missing"
                await conn.execute(
                    "DELETE FROM memories "
                    " WHERE scope_kind = $1 AND scope_id = $2 AND key = $3",
                    scope_kind,
                    scope_id,
                    key,
                )
                return "deleted"
            await conn.execute(
                """
                INSERT INTO memories
                    (scope_kind, scope_id, key, content, mem_type,
                     confidence, source_message_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (scope_kind, scope_id, key) DO UPDATE SET
                    content = EXCLUDED.content,
                    mem_type = EXCLUDED.mem_type,
                    confidence = GREATEST(memories.confidence, EXCLUDED.confidence),
                    source_message_id = EXCLUDED.source_message_id,
                    updated_at = now(),
                    last_confirmed_at = now()
                """,
                scope_kind,
                scope_id,
                key,
                content,
                mem_type,
                confidence,
                source_message_id,
            )
            return "updated" if exists else "created"

    async def list_memories(
        self,
        *,
        user_id: str,
        group_id: str | None,
        fresh_within_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """fresh_within_days 非空时只返回近期被确认过的记忆（陈旧记忆不注入）。"""
        rows = await self.pool.fetch(
            """
            SELECT scope_kind, scope_id, key, content, mem_type, confidence,
                   last_confirmed_at
              FROM memories
             WHERE ((scope_kind = 'user' AND scope_id = $1)
                 OR (scope_kind = 'group' AND scope_id = $2))
               AND ($3::int IS NULL
                    OR last_confirmed_at >= now() - make_interval(days => $3))
             ORDER BY confidence DESC, last_confirmed_at DESC
            """,
            user_id,
            group_id,
            fresh_within_days,
        )
        return [dict(r) for r in rows]

    async def search_memories(
        self, *, user_id: str, group_id: str | None, query: str
    ) -> list[dict[str, Any]]:
        pattern = f"%{query}%"
        rows = await self.pool.fetch(
            """
            SELECT scope_kind, scope_id, key, content, mem_type, confidence
              FROM memories
             WHERE ((scope_kind = 'user' AND scope_id = $1)
                 OR (scope_kind = 'group' AND scope_id = $2))
               AND (key ILIKE $3 OR content ILIKE $3)
             ORDER BY confidence DESC, last_confirmed_at DESC
             LIMIT 20
            """,
            user_id,
            group_id,
            pattern,
        )
        return [dict(r) for r in rows]

    async def usage_summary(self, day: str) -> dict[str, int]:
        """按自然日（Asia/Shanghai）聚合 assistant 消息上的用量。"""
        row = await self.pool.fetchrow(
            """
            SELECT coalesce(sum(prompt_tokens), 0)     AS prompt_tokens,
                   coalesce(sum(cached_tokens), 0)     AS cached_tokens,
                   coalesce(sum(completion_tokens), 0) AS completion_tokens,
                   coalesce(sum(llm_calls), 0)         AS calls,
                   count(*) FILTER (WHERE llm_calls > 0) AS turns
              FROM messages
             WHERE role = 'assistant'
               AND (created_at AT TIME ZONE 'Asia/Shanghai')::date = $1::text::date
            """,
            day,
        )
        return {key: int(value) for key, value in dict(row).items()}

    async def refresh_member_name(
        self, *, group_id: str, user_id: str, card: str, nickname: str
    ) -> bool:
        """群名片/昵称变化时增量更新（改名在每条消息上即时生效）。"""
        row = await self.pool.fetchrow(
            "SELECT card, nickname FROM group_members "
            " WHERE group_id = $1 AND user_id = $2",
            group_id,
            user_id,
        )
        if row and (row["card"] or "") == card and (row["nickname"] or "") == nickname:
            return False
        await self.upsert_members(
            group_id, [{"user_id": user_id, "card": card, "nickname": nickname}]
        )
        return True

    # ── 外部身份映射（QQ 官方 openid）────────────────────

    async def upsert_sticker(
        self,
        *,
        sha256: str,
        path: str,
        name: str,
        tags: list[str],
        description: str,
    ) -> str:
        """按 sha256 幂等导入；返回 created / updated。"""
        async with self.pool.acquire() as conn, conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM stickers WHERE sha256 = $1", sha256
            )
            await conn.execute(
                """
                INSERT INTO stickers (sha256, path, name, tags, description)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (sha256) DO UPDATE SET
                    path = EXCLUDED.path,
                    name = EXCLUDED.name,
                    tags = EXCLUDED.tags,
                    description = EXCLUDED.description,
                    enabled = TRUE
                """,
                sha256,
                path,
                name,
                tags,
                description,
            )
            return "updated" if exists else "created"

    async def list_stickers(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, sha256, path, name, tags, description, enabled,
                   usage_count, last_used_at
              FROM stickers
             WHERE enabled
             ORDER BY usage_count DESC, name
            """
        )
        return [dict(r) for r in rows]

    async def get_sticker_by_name(self, name: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT id, sha256, path, name, tags, description, enabled, usage_count
              FROM stickers
             WHERE lower(name) = lower($1) AND enabled
             LIMIT 1
            """,
            name,
        )
        return dict(row) if row else None

    async def bump_sticker_usage(self, sticker_id: int) -> None:
        await self.pool.execute(
            "UPDATE stickers SET usage_count = usage_count + 1, "
            "       last_used_at = now() WHERE id = $1",
            sticker_id,
        )

    async def disable_stickers_except(self, paths: list[str]) -> int:
        """把磁盘上已不存在的表情标记为停用（不删除记录，便于恢复）。"""
        status = await self.pool.execute(
            "UPDATE stickers SET enabled = FALSE WHERE NOT (path = ANY($1::text[]))",
            paths,
        )
        return int(status.split()[-1])

    # ── 视频笔记缓存 ─────────────────────────────────────

    async def get_video_note(self, video_key: str) -> dict[str, Any] | None:
        """按规范化视频键取笔记；命中时累加 hit_count。"""
        row = await self.pool.fetchrow(
            """
            SELECT id, video_key, platform, video_id, source_url, title,
                   duration, note, frame_count, subtitle_chars, metadata,
                   hit_count, created_at, updated_at, last_used_at
              FROM video_notes
             WHERE video_key = $1
            """,
            video_key,
        )
        if not row:
            return None
        await self.pool.execute(
            "UPDATE video_notes SET hit_count = hit_count + 1, "
            "       last_used_at = now() WHERE video_key = $1",
            video_key,
        )
        result = dict(row)
        result["hit_count"] = int(result["hit_count"]) + 1
        return result

    async def save_video_note(  # noqa: PLR0913
        self,
        *,
        video_key: str,
        platform: str,
        video_id: str = "",
        source_url: str = "",
        title: str = "",
        duration: float = 0.0,
        note: str,
        frame_count: int = 0,
        subtitle_chars: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """新增或覆盖视频笔记（同一视频只保留最新一份）。"""
        await self.pool.execute(
            """
            INSERT INTO video_notes
                (video_key, platform, video_id, source_url, title, duration,
                 note, frame_count, subtitle_chars, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (video_key) DO UPDATE SET
                platform = EXCLUDED.platform,
                video_id = EXCLUDED.video_id,
                source_url = EXCLUDED.source_url,
                title = EXCLUDED.title,
                duration = EXCLUDED.duration,
                note = EXCLUDED.note,
                frame_count = EXCLUDED.frame_count,
                subtitle_chars = EXCLUDED.subtitle_chars,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """,
            video_key,
            platform,
            video_id,
            source_url,
            title,
            duration,
            note,
            frame_count,
            subtitle_chars,
            metadata or {},
        )

    # ── 群术语库 ─────────────────────────────────────────

    async def upsert_glossary_term(
        self,
        *,
        group_id: str,
        term: str,
        term_key: str,
        definition: str,
        created_by: int | None = None,
    ) -> str:
        """新增/更新词条，返回 created / updated。"""
        async with self.pool.acquire() as conn, conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM glossary_terms WHERE group_id = $1 AND term_key = $2",
                group_id,
                term_key,
            )
            await conn.execute(
                """
                INSERT INTO glossary_terms
                    (group_id, term, term_key, definition, created_by)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (group_id, term_key) DO UPDATE SET
                    term = EXCLUDED.term,
                    definition = EXCLUDED.definition,
                    updated_at = now()
                """,
                group_id,
                term,
                term_key,
                definition,
                created_by,
            )
            return "updated" if exists else "created"

    async def list_glossary(self, group_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT term, term_key, definition, created_at, updated_at
              FROM glossary_terms
             WHERE group_id = $1
             ORDER BY length(term_key) DESC
            """,
            group_id,
        )
        return [dict(r) for r in rows]

    async def purge_messages(self, days: int) -> int:
        """删除超过保留期的原文（摘要已覆盖的部分随之消失，摘要本身保留）。"""
        status = await self.pool.execute(
            "DELETE FROM messages WHERE created_at < now() - make_interval(days => $1)",
            days,
        )
        return int(status.split()[-1])

    async def purge_summaries(self, days: int) -> int:
        """删除过期摘要，但每组保留最新一条（否则该群会失去全部背景）。"""
        status = await self.pool.execute(
            """
            DELETE FROM group_summaries s
             WHERE s.created_at < now() - make_interval(days => $1)
               AND s.id <> (
                   SELECT max(x.id) FROM group_summaries x
                    WHERE x.group_id = s.group_id
               )
            """,
            days,
        )
        return int(status.split()[-1])

    async def memory_stats(self) -> dict[str, int]:
        row = await self.pool.fetchrow(
            """
            SELECT (SELECT count(*) FROM messages)        AS messages,
                   (SELECT count(*) FROM group_summaries) AS summaries,
                   (SELECT count(*) FROM memories)        AS memories
            """
        )
        return {key: int(value) for key, value in dict(row).items()}

    @staticmethod
    def _to_message(row: asyncpg.Record) -> ChatMessage:
        return ChatMessage(
            role=row["role"],
            sender_name=row["sender_name"],
            content=row["content"],
            tool_call_id=row["tool_call_id"],
            tool_calls=row["tool_calls"],
        )
