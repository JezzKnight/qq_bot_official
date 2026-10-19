-- v003：表情包库（文件留在磁盘，表只存元数据与路径）
CREATE TABLE stickers (
    id            BIGSERIAL PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,          -- 去重键：同一张图只存一条
    path          TEXT NOT NULL,                 -- 相对 stickers 目录的路径
    name          TEXT NOT NULL UNIQUE,           -- 模型调用用的名称
    tags          TEXT[] NOT NULL DEFAULT '{}',   -- 语义标签
    description   TEXT NOT NULL DEFAULT '',       -- 使用时机描述（给模型判断）
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    usage_count   INT NOT NULL DEFAULT 0,
    last_used_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_stickers_tags ON stickers USING GIN (tags);
