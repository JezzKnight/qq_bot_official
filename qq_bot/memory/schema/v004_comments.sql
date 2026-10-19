-- v004：为所有表与字段补充中文注释（表/字段备注）
-- 说明：COMMENT ON 是幂等的，重复执行只会覆盖为相同内容

COMMENT ON TABLE schema_version IS 'schema 迁移版本记录（migrate.py 据此增量应用 schema/*.sql）';
COMMENT ON COLUMN schema_version.version IS '已应用的最高版本号';
COMMENT ON COLUMN schema_version.applied_at IS '该版本应用时间';

COMMENT ON TABLE users IS '全局用户表：以不可变 QQ 号为键；display_name 只是展示缓存';
COMMENT ON COLUMN users.user_id IS 'QQ 号（不可变身份键，所有层以此关联）';
COMMENT ON COLUMN users.display_name IS '最近一次见到的显示名（群名片优先，仅展示，可变）';
COMMENT ON COLUMN users.first_seen IS '首次出现时间';
COMMENT ON COLUMN users.last_seen IS '最近出现时间';

COMMENT ON TABLE group_members IS '群成员关系：同一用户在不同群可有不同名片';
COMMENT ON COLUMN group_members.group_id IS '群号';
COMMENT ON COLUMN group_members.user_id IS '成员 QQ 号';
COMMENT ON COLUMN group_members.card IS '群名片（可变，仅展示与匹配）';
COMMENT ON COLUMN group_members.nickname IS 'QQ 昵称（可变）';
COMMENT ON COLUMN group_members.updated_at IS '最近一次同步时间';

COMMENT ON TABLE user_names IS '称呼与别名：本人要求的称呼、历史旧名、群名片/昵称同步（生成侧查 call，理解侧查 resolve）';
COMMENT ON COLUMN user_names.id IS '自增主键';
COMMENT ON COLUMN user_names.user_id IS '该称呼指向的用户';
COMMENT ON COLUMN user_names.group_id IS '作用域：NULL=全局，否则仅该群生效';
COMMENT ON COLUMN user_names.name IS '称呼原文（展示用）';
COMMENT ON COLUMN user_names.name_key IS '归一化键（NFKC/折叠空白/小写），匹配与唯一性依据';
COMMENT ON COLUMN user_names.is_preferred IS '是否本人明确要求的称呼（生成时优先使用）';
COMMENT ON COLUMN user_names.source IS '来源：user=本人设定 / card=群名片同步 / nickname=昵称同步';
COMMENT ON COLUMN user_names.weight IS '冲突优先序，越小越优先（preferred 10 < 别名 50 < 名片 100 < 昵称 110）';
COMMENT ON COLUMN user_names.updated_at IS '最近更新时间';

COMMENT ON TABLE threads IS '对话线程：每成员每群一条，私聊按用户一条';
COMMENT ON COLUMN threads.id IS '线程主键（messages.thread_id 外键）';
COMMENT ON COLUMN threads.kind IS '线程类型：group_member=群内成员线程 / private=私聊';
COMMENT ON COLUMN threads.group_id IS '群号；私聊为 NULL';
COMMENT ON COLUMN threads.user_id IS '线程所属成员 QQ 号';
COMMENT ON COLUMN threads.last_active_at IS '最近活跃时间（启动补扫与清理参考）';

COMMENT ON TABLE messages IS 'L1 工作记忆：原始消息（唯一真相，摘要只是派生物）';
COMMENT ON COLUMN messages.id IS '消息主键（摘要区间 covers_from/covers_to 以此为准）';
COMMENT ON COLUMN messages.thread_id IS '所属线程（个人线程窗口用）';
COMMENT ON COLUMN messages.group_id IS '群号（群窗口与按群压缩用）；私聊为 NULL';
COMMENT ON COLUMN messages.sender_id IS '发言人 QQ 号；bot 发言为 NULL';
COMMENT ON COLUMN messages.sender_name IS '发言时的显示名（历史保真；OpenAI name 字段使用）';
COMMENT ON COLUMN messages.role IS '角色：user / assistant / tool';
COMMENT ON COLUMN messages.content IS '消息正文（用户消息含 <user identity> 标签）';
COMMENT ON COLUMN messages.tool_calls IS '工具调用载荷（JSONB，回放历史时原样回传）';
COMMENT ON COLUMN messages.tool_call_id IS '工具结果消息回指的调用 id';
COMMENT ON COLUMN messages.images IS '图片元信息引用（JSONB，不存二进制）';
COMMENT ON COLUMN messages.token_est IS '入库时的 token 估算（中文按字、英文按词）';
COMMENT ON COLUMN messages.created_at IS '发送时间（timestamptz）';

COMMENT ON TABLE group_summaries IS 'L2 情节记忆：群聊摘要（只有群聊会压缩，个人线程不压缩）';
COMMENT ON COLUMN group_summaries.id IS '自增主键';
COMMENT ON COLUMN group_summaries.group_id IS '群号';
COMMENT ON COLUMN group_summaries.content IS '摘要正文';
COMMENT ON COLUMN group_summaries.covers_from IS '覆盖的起始 message.id';
COMMENT ON COLUMN group_summaries.covers_to IS '覆盖的结束 message.id（取最大者作为当前摘要，也是幂等键）';
COMMENT ON COLUMN group_summaries.token_est IS '摘要 token 估算';
COMMENT ON COLUMN group_summaries.created_at IS '生成时间';

COMMENT ON TABLE memories IS 'L3 语义记忆：稳定事实/偏好/群规矩（带置信度与溯源）';
COMMENT ON COLUMN memories.id IS '自增主键';
COMMENT ON COLUMN memories.scope_kind IS '作用域类型：user=个人 / group=群公共';
COMMENT ON COLUMN memories.scope_id IS '作用域 id：user_id 或 group_id';
COMMENT ON COLUMN memories.key IS '记忆标识（同一 key 覆盖更新）';
COMMENT ON COLUMN memories.content IS '记忆正文（第三人称客观陈述）';
COMMENT ON COLUMN memories.mem_type IS '类型：fact / preference / knowledge / rule';
COMMENT ON COLUMN memories.confidence IS '置信度 0~1（模型写入 0.7、用户确认 0.9、自动固化 0.5）';
COMMENT ON COLUMN memories.source_message_id IS '溯源：支撑该记忆的 message.id';
COMMENT ON COLUMN memories.created_at IS '创建时间';
COMMENT ON COLUMN memories.updated_at IS '最近更新时间';
COMMENT ON COLUMN memories.last_confirmed_at IS '最近确认时间（超期不再注入，但不删除）';

COMMENT ON TABLE glossary_terms IS '群术语库：特定词语的释义（按群隔离）';
COMMENT ON COLUMN glossary_terms.id IS '自增主键';
COMMENT ON COLUMN glossary_terms.group_id IS '群号';
COMMENT ON COLUMN glossary_terms.term IS '词汇原文';
COMMENT ON COLUMN glossary_terms.term_key IS '归一化键（NFKC/折叠空白/小写），唯一性依据';
COMMENT ON COLUMN glossary_terms.definition IS '第三人称客观释义';
COMMENT ON COLUMN glossary_terms.created_by IS '补充该词条的用户 QQ 号（可空）';
COMMENT ON COLUMN glossary_terms.created_at IS '创建时间';
COMMENT ON COLUMN glossary_terms.updated_at IS '最近更新时间';

COMMENT ON TABLE stickers IS '表情包库：文件留磁盘，表存元数据与使用统计';
COMMENT ON COLUMN stickers.id IS '自增主键';
COMMENT ON COLUMN stickers.sha256 IS '文件内容哈希（去重键，同一张图只存一条）';
COMMENT ON COLUMN stickers.path IS '相对 stickers 目录的路径';
COMMENT ON COLUMN stickers.name IS '表情包名称（模型调用 send_sticker 使用，唯一）';
COMMENT ON COLUMN stickers.tags IS '语义标签数组（无语、震惊等，用于解析）';
COMMENT ON COLUMN stickers.description IS '使用时机描述（给模型判断是否该发）';
COMMENT ON COLUMN stickers.enabled IS '是否启用（文件缺失时自动停用）';
COMMENT ON COLUMN stickers.usage_count IS '累计发送次数';
COMMENT ON COLUMN stickers.last_used_at IS '最近发送时间';
COMMENT ON COLUMN stickers.created_at IS '导入时间';
