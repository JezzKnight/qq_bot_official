-- v007：外部身份 → 内部 id 映射（QQ 开放平台用 openid，QQ 号体系之外）
CREATE SEQUENCE IF NOT EXISTS identity_seq MINVALUE 1000000000;

CREATE TABLE identity_map (
    platform    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('user', 'group')),
    external_id TEXT NOT NULL,
    internal_id BIGINT NOT NULL UNIQUE DEFAULT nextval('identity_seq'),
    display_name TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, kind, external_id)
);

CREATE INDEX ix_identity_internal ON identity_map (internal_id);

COMMENT ON TABLE identity_map IS '外部平台身份到内部 id 的映射（QQ 官方 openid 与 QQ 号体系隔离）';
COMMENT ON COLUMN identity_map.platform IS '来源平台：onebot / qq';
COMMENT ON COLUMN identity_map.kind IS '身份类型：user=用户，group=群';
COMMENT ON COLUMN identity_map.external_id IS '外部标识：openid 或群 openid';
COMMENT ON COLUMN identity_map.internal_id IS '内部分配的 id（序列从 10 亿起，避开真实 QQ 号段）';
COMMENT ON COLUMN identity_map.display_name IS '最近一次见到的显示名（官方接口可能为空）';
COMMENT ON COLUMN identity_map.created_at IS '首次映射时间';
COMMENT ON COLUMN identity_map.updated_at IS '最近更新时间';
