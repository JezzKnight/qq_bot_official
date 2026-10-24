# docs/spec

| 文档 | 内容 |
|---|---|
| [`group-presence.md`](group-presence.md) | 群聊存在感（Presence）与自动接话：状态机、0-1 抽奖、窗口、双冷却、静默、黑名单、可接性过滤、复读机、参数表、测试矩阵、迁移与分期 |
| [`group-conversation.md`](group-conversation.md) | 群聊会话图：发言账本、指向解析、`[群聊状态]` 注入、参与策略（P1、P2 已实现） |
| [`state-machine.md`](state-machine.md) | 存在感状态机的 ASCII 图解：三态总图、三条入口管线、转移表、退出原因与参数快照 |

当前进度：**v1.14，P1 + P2 + P3 均已实现**（状态机 / 抽奖 / 窗口 / 双冷却 / 静默 / 黑名单 / 可接性 / 复读机 / 关键词三语境 + 回钩 / 窗口内三态判定）；群聊会话图 **P1 + P2** 已实现（发言账本 / 指向解析 / `[群聊状态]` 注入 / 参与策略）。

版本变化：

- v1.1：纯图片与短句（「难绷」这类）改为**可接**；纯表情包仍不接；新增纯代码的**复读机**。
- v1.2：复读机**不做单独限频**，支持「@某人 + 内容」并按原文重建 @ 段，**严格原样**；纯图片抽奖概率乘 `IMAGE_RATE_MULT`（默认 0.5）。
- v1.3：关键词触发改为「大肥鱼」**精确匹配**，先分 `CALL` / `ABOUT` / `HOMONYM` 三种语境；`ABOUT` 走过渡句 + 回钩，`HOMONYM` 默认不打扰。
- v1.4：`HOMONYM` 默认跳过；过渡句由模型按人设生成（读群窗口 + 群摘要，不读个人线程）；复读计数按不同用户去重；昵称取不到用「有人」。
- v1.5：复读走**主动消息**且**不占用** `REPLY_COOLDOWN`；黑名单也挡关键词路径；过渡兜底句固定「什么意思」；过渡上下文默认 15 条。
- v1.6：`@` **不关闭**已开启的 `AWAKE` 窗口；静默词维持三个不扩；参数表补齐说明。
- v1.7：按 5 人小群校准——`RATE=0.2`、`RATE_IN_WINDOW=2.0`（窗口内实际 0.4）。
- v1.8：删除 `WINDOW_REPLIES`（窗口内不再限条数，靠概率 × 冷却 × 日上限）；`DAILY_ACTIVE` 提到 120。
- v1.9：P1 落地（`qq_bot/chat/presence.py` + 薄适配层 `autotrigger.py`），`activation.py` 退役，新增 `tests/test_presence.py`（36 例）。
- v1.10：P2 落地——`qq_bot/chat/relevance.py` 三语境判定 + 过渡句生成、`prompts/chat/keyword_transition.md`、回钩与关键词冷却；`tests/test_relevance.py`（9 例）与 presence 追加 10 例。
- v1.12：`AWAKE` 在场时看到消息 @ 了别人，立即退回 `SLEEPING` 并进入唤醒冷却（话题已经交给别人）。
- v1.11：实盘降噪——关键词 `CALL` 只回一次、不再开 `AWAKE` 窗口；`REPLY_COOLDOWN` 默认 45 秒（`.env` 60）；`prompts/chat/group.md` 增加「先判断在跟谁说」的群聊发言规则。
- v1.14：关键词 `CALL`（叫名字）改为进入 600 秒 `AWAKE`，退出由 §6.10 三态判定控制、窗口时长兜底；`ABOUT` 仍用 300 秒短窗。
- v1.13：P3 窗口内三态判定——`qq_bot/chat/judge.py` 判 `REPLY/PASS/SLEEP`；连续 `PASS` 达到上限退场，判定失败一律 `PASS`；`CALL` 在窗口内强制 `REPLY`。

已知约束（来自官方文档）：

- 被动消息：5 分钟有效，每条消息最多回复 5 次。
- 主动消息：个人认证 60 条/分钟（Bot 维度）、单群 20 条/分钟、每群每天 1000 条。

评审待定项见 [`group-presence.md` 第 14 节](group-presence.md#14-开放问题评审时定)。

- 会话图 v3：P2 参与策略落地——`qq_bot/chat/participation.py` 在 judge 前接管明确场景（叫 bot 必回、@ 别人不插嘴、话题无关 PASS），模糊情况仍交回 presence；`GROUP_PRESENCE_POLICY_ENABLED` 可整体回退。
