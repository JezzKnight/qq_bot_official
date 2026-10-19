-- v009：用户/群 ID 改为官方 openid（TEXT），删除内部数字 ID 与 identity_map
-- 说明：本迁移假设业务数据已清空（QQ-only 新库）；旧数字 ID 不保留映射。
ALTER TABLE users           ALTER COLUMN user_id   TYPE TEXT USING user_id::text;
ALTER TABLE group_members   ALTER COLUMN group_id  TYPE TEXT USING group_id::text;
ALTER TABLE group_members   ALTER COLUMN user_id   TYPE TEXT USING user_id::text;
ALTER TABLE user_names      ALTER COLUMN user_id   TYPE TEXT USING user_id::text;
ALTER TABLE user_names      ALTER COLUMN group_id  TYPE TEXT USING group_id::text;
ALTER TABLE threads         ALTER COLUMN group_id  TYPE TEXT USING group_id::text;
ALTER TABLE threads         ALTER COLUMN user_id   TYPE TEXT USING user_id::text;
ALTER TABLE messages        ALTER COLUMN group_id  TYPE TEXT USING group_id::text;
ALTER TABLE messages        ALTER COLUMN sender_id TYPE TEXT USING sender_id::text;
ALTER TABLE memories        ALTER COLUMN scope_id  TYPE TEXT USING scope_id::text;
ALTER TABLE group_summaries ALTER COLUMN group_id  TYPE TEXT USING group_id::text;
ALTER TABLE glossary_terms  ALTER COLUMN group_id  TYPE TEXT USING group_id::text;

DROP TABLE IF EXISTS identity_map;
DROP SEQUENCE IF EXISTS identity_seq;
