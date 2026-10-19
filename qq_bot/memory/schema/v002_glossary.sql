-- v002：群术语库（原 long_term_memory/groups/{gid}/glossary.json）
CREATE TABLE glossary_terms (
    id          BIGSERIAL PRIMARY KEY,
    group_id    BIGINT NOT NULL,
    term        TEXT NOT NULL,               -- 词汇原样
    term_key    TEXT NOT NULL,               -- 归一化键（NFKC/折叠空白/小写），唯一性依据
    definition  TEXT NOT NULL,               -- 第三人称客观释义
    created_by  BIGINT,                      -- 谁补充的词条（可空）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (group_id, term_key)
);

CREATE INDEX ix_glossary_group ON glossary_terms (group_id);
