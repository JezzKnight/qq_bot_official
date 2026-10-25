-- 摘要记录覆盖的时间范围：注入时告诉模型这段摘要是什么时候的事。
ALTER TABLE group_summaries ADD COLUMN IF NOT EXISTS covers_from_at TIMESTAMPTZ;
ALTER TABLE group_summaries ADD COLUMN IF NOT EXISTS covers_to_at   TIMESTAMPTZ;

COMMENT ON COLUMN group_summaries.covers_from_at IS '摘要覆盖的第一条消息时间';
COMMENT ON COLUMN group_summaries.covers_to_at   IS '摘要覆盖的最后一条消息时间';

-- 历史摘要回填：按 message.id 区间取最早/最晚时间。
UPDATE group_summaries s
   SET covers_from_at = (
         SELECT min(m.created_at) FROM messages m
          WHERE m.group_id = s.group_id AND m.id BETWEEN s.covers_from AND s.covers_to
       ),
       covers_to_at = (
         SELECT max(m.created_at) FROM messages m
          WHERE m.group_id = s.group_id AND m.id BETWEEN s.covers_from AND s.covers_to
       )
 WHERE s.covers_from_at IS NULL OR s.covers_to_at IS NULL;
