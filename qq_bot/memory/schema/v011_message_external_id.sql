-- 平台消息 id：同一条群消息可能先被 _record_full_message 落库，再被 turn 落库一次，
-- 用 (thread_id, external_id) 唯一约束让第二次插入变成 no-op。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS external_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_msg_external
    ON messages (thread_id, external_id)
    WHERE external_id IS NOT NULL;

COMMENT ON COLUMN messages.external_id IS '平台侧消息 id，用于同一事件重复落库时去重';
