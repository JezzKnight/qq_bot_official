-- v005：把 token 用量记到消息上（一轮对话的合计，含工具循环与子 Agent）
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS prompt_tokens     INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS completion_tokens INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cached_tokens     INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS llm_calls         INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS model             TEXT;

COMMENT ON COLUMN messages.prompt_tokens IS '本轮所有 LLM 调用的输入 token 合计（含工具循环与子 Agent）';
COMMENT ON COLUMN messages.completion_tokens IS '本轮所有 LLM 调用的输出 token 合计';
COMMENT ON COLUMN messages.cached_tokens IS '其中命中服务端缓存的输入 token';
COMMENT ON COLUMN messages.llm_calls IS '本轮实际发生的 LLM 调用次数（含子 Agent）';
COMMENT ON COLUMN messages.model IS '本轮主模型名称（仅 assistant 消息有值）';
