-- v001_init：记忆子系统初始 schema
-- 来源：docs/memory-design.md §5（已在 PostgreSQL 18.6 上验证）
-- 注意：schema_version 表由 migrate.py 的 bootstrap 创建，这里不再重复建
-- 变更方式：新增 v002_*.sql，不要修改本文件（migrate.py 按 schema_version 增量应用）

CREATE TABLE users (
    user_id      BIGINT PRIMARY KEY,
    display_name TEXT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE group_members (
    group_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    card       TEXT,
    nickname   TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);

-- 称呼与别名（一张表同时承载：本人希望被称呼的名字、他人的别名、群名片/昵称同步）
-- 取代上一版草稿里的 member_aliases：避免"两张表管同一件事"
CREATE TABLE user_names (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    group_id     BIGINT,                      -- NULL = 全局生效（跨群）
    name         TEXT NOT NULL,               -- 原始写法
    name_key     TEXT NOT NULL,               -- 归一化键（去空白/全角转半角/小写）
    is_preferred BOOLEAN NOT NULL DEFAULT FALSE,  -- 是否"本人希望被这么称呼"
    source       TEXT NOT NULL,               -- user | admin | card | nickname
    weight       INT NOT NULL,                -- preferred 10 < alias 50 < card 100 < nickname 110
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (user_id, group_id, name_key)
);
CREATE INDEX ix_names_lookup    ON user_names (group_id, name_key);
CREATE INDEX ix_names_preferred ON user_names (user_id) WHERE is_preferred;

-- ① "先到先得"：同一个 preferred 名字在作用域内只能属于一个人
CREATE UNIQUE INDEX ux_names_pref_global ON user_names (name_key)
    WHERE is_preferred AND group_id IS NULL;
CREATE UNIQUE INDEX ux_names_pref_group  ON user_names (group_id, name_key)
    WHERE is_preferred AND group_id IS NOT NULL;

-- ② 每人最多一个 preferred、最多一个"本人设定的旧别名"（群名片同步行 source='card' 不计入）
CREATE UNIQUE INDEX ux_names_one_preferred ON user_names (user_id, group_id)
    NULLS NOT DISTINCT WHERE is_preferred;
CREATE UNIQUE INDEX ux_names_one_alias ON user_names (user_id, group_id)
    NULLS NOT DISTINCT WHERE source = 'user' AND NOT is_preferred;

CREATE TABLE threads (
    id             BIGSERIAL PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('group_member', 'private')),
    group_id       BIGINT,
    user_id        BIGINT NOT NULL,
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (kind, group_id, user_id)
);

CREATE TABLE messages (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   BIGINT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    group_id    BIGINT,                 -- 冗余列：群窗口/压缩直接按它查
    sender_id   BIGINT,                 -- 发言 uid；bot 为 NULL
    sender_name TEXT,                   -- 发言时的显示名（历史保真；OpenAI name 字段要用）
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content     TEXT,
    tool_calls  JSONB,
    tool_call_id TEXT,                  -- 工具结果消息的回指（OpenAI 协议要求）
    images      JSONB,                  -- 只存引用，不存二进制
    token_est   INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_msg_thread_recent ON messages (thread_id, id DESC);
CREATE INDEX ix_msg_group_recent  ON messages (group_id, id DESC);

CREATE TABLE group_summaries (
    id          BIGSERIAL PRIMARY KEY,
    group_id    BIGINT NOT NULL,
    content     TEXT NOT NULL,
    covers_from BIGINT NOT NULL,
    covers_to   BIGINT NOT NULL,
    token_est   INT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (group_id, covers_to)        -- 幂等：同一批消息只摘要一次
);
CREATE INDEX ix_summary_latest ON group_summaries (group_id, covers_to DESC);

CREATE TABLE memories (
    id                BIGSERIAL PRIMARY KEY,
    scope_kind        TEXT NOT NULL CHECK (scope_kind IN ('user', 'group')),
    scope_id          BIGINT NOT NULL,
    key               TEXT NOT NULL,
    content           TEXT NOT NULL,
    mem_type          TEXT NOT NULL,     -- fact / preference / knowledge / rule
    confidence        REAL NOT NULL DEFAULT 0.7,
    source_message_id BIGINT,            -- 溯源
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_kind, scope_id, key)
);
CREATE INDEX ix_mem_scope ON memories (scope_kind, scope_id, confidence DESC);
