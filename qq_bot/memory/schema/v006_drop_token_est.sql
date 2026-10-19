-- v006：删除 messages.token_est（用量已改由 prompt/completion/cached/llm_calls 表达）
ALTER TABLE messages DROP COLUMN IF EXISTS token_est;
