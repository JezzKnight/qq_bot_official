# 群聊会话图（GroupConversation）与指向解析 · 设计规格

| 项 | 内容 |
|---|---|
| 版本 | v4（移除 [群聊状态]，历史改标签） |
| 日期 | 2026-10-25 |
| 状态 | P1、P2 已实现；`[群聊状态]` 注入已移除（全量 277 passed） |
| 前置 | [`group-presence.md`](group-presence.md) v1.14（状态机 / 窗口 / judge） |
| 关联 | `qq_bot/chat/presence.py`、`qq_bot/chat/autotrigger.py`、`qq_bot/chat/turn.py` |

## 1. 背景与问题

真实记录（测试群，bot 名「雷霆大肥鱼」）：

```
隐身人：@雷霆大肥鱼 fw
隐身人：@雷霆大肥鱼 fw
隐身人：@雷霆大肥鱼 fw
AMelax：@雷霆大肥鱼 废物
AMelax：@雷霆大肥鱼 废物
```

bot 的回复把两个人的次数**合并**成「第四遍/第五遍」；被「你分得清谁是谁不」质疑后，又说「我以为重复是另一个人，是我笨」，越解释越乱。

同一场景用本仓库真实 `run_turn` 回放，结论一致：

- **逐条消息的发送者标签是准的**（模型能看到每条消息的 name）；
- **跨消息聚合计数是错的**（3 次说成 2 次）；
- **被质疑时会自我推翻**（模型把「重复」解释成「另一个人」，而不是查证）。

根因不在 prompt 措辞，而在三件事被交给了语言模型：

1. **聚合**：上下文是平铺文本，模型不擅长跨多轮计数、按说话人归并；
2. **指向**：「这条在跟谁说话」需要 @ 关系、引用关系、上一句是谁说的——这些是硬事实，现在让模型猜；
3. **稳定性**：prompt 规则在用户质疑下会被「顺从」覆盖，没有代码层的事实兜底。

结论：**prompt 管措辞，归属和指向必须由代码算好再喂给模型。**

## 2. 目标与非目标

### 目标（P1）

- 每群维护一份**会话图**：消息、发送者、@/引用关系、按人聚合的发言账本。
- 代码解析**当前消息指向谁**（bot / 某个用户 / 群里），输出结构化结果。
- 把可信的**归属事实**以一行 `[群聊状态]` 注入 prompt，所有群聊回复都带。
- 不加依赖、不改数据库、不改现有回复判定。

### 非目标（P1）

- 不做影子模式；参与策略在 P2 直接接管（见 §6）。
- 不做针对「计数/辱骂」场景的专用分支（该场景只是举例，不是高频需求）。
- 不做跨群用户画像、不做持久化新表（重启重建放 P3）。

## 3. 总体架构

```
每条群消息（@ / 非@ / 关键词）
        │
        ▼
┌─────────────────────────────────────────────┐
│ GroupConversation（每群一份，内存）          │
│                                             │
│  MessageRing  最近 30 条 MessageNode        │
│  SpeakerLedger 按人聚合的发言账本           │
│  MentionGraph  @ / 引用 关系                │
│  TopicState    话题词 + 时间衰减            │
└───────────────┬─────────────────────────────┘
                │ 只读查询
                ▼
        ┌───────────────┐        ┌──────────────────┐
        │ Addressee     │        │ [群聊状态] 注入   │
        │ Resolver      │───────▶│ turn.py system   │
        │ (在跟谁说话)   │        │ prompt 尾部       │
        └───────────────┘        └──────────────────┘
```

P2 会在这里加一个 `ParticipationPolicy`（回复 / 过 / 睡）消费同一份图，P1 不实现。

## 4. 数据模型

### 4.1 MessageNode

```python
@dataclass(slots=True)
class MessageNode:
    mid: str                    # 平台 message_id
    sender_id: str              # openid；bot 自己用 bot_id
    sender_name: str            # 群名片/昵称，取不到用「有人」
    text: str                   # 原文（不含引用前缀）
    at: float                   # 时间戳（time.monotonic 或 wall clock，统一一种）
    mentions: tuple[str, ...]   # @ 的 openid 序列（含 bot）
    mention_bot: bool           # 是否 @ 了 bot
    reply_to: str | None        # 引用/回复的消息 id（拿不到为 None）
    tokens: frozenset[str]      # 归一化后的 2-gram，话题用
```

### 4.2 SpeakerStat / SpeakerLedger

```python
@dataclass(slots=True)
class SpeakerStat:
    user_id: str
    name: str
    count: int                   # 窗口内发言条数
    texts: Counter[str]          # 归一化文本 → 次数（"fw" → 3）
    last_at: float

class SpeakerLedger:
    def observe(self, node: MessageNode) -> None: ...
    def stats(self, since_sec: float = 600.0, now: float | None = None) -> list[SpeakerStat]: ...
    def count_of(self, user_id: str, text: str | None = None) -> int: ...
    def who_said(self, text: str) -> list[tuple[str, int]]: ...
```

归一化：`strip` → 全角转半角 → 小写 → 连续空白压成一个。`fw` / `FW` / `ｆｗ` 视为同一个。

### 4.3 Addressee

```python
@dataclass(frozen=True, slots=True)
class Addressee:
    target: str        # "bot" | openid | "group"
    score: float       # 0~1
    evidence: tuple[str, ...]   # 命中的信号，供日志与调试
```

## 5. AddresseeResolver：这条在跟谁说话

输入：当前 `MessageNode`、最近若干 `MessageNode`、bot 的 openid、bot 名字列表。
输出：最高分的 `Addressee`。

### 5.1 信号与分值

| # | 信号 | 指向 | 分值 | 备注 |
|---|---|---|---|---|
| 1 | 消息 @ 了 bot | bot | 1.00 | `mention_bot=True` |
| 2 | 引用的消息发送者是 bot | bot | 0.95 | 拿不到引用目标时跳过 |
| 3 | 文本含 bot 名字且 `relevance.fast_kind == CALL` | bot | 0.90 | 复用 `qq_bot/chat/relevance.py` |
| 4 | 含「你 / 你们」且上一句发送者是 bot | bot | 0.75 | 代词指向 |
| 5 | 紧接 bot 发言后的第一句是疑问句 | bot | 0.60 | 疑问句式：吗/呢/？/吗？ |
| 6 | 消息 @ 了某个用户（非 bot） | 该用户 | 1.00 | 同时 @bot 时 bot 优先 |
| 7 | 最近 3 条是 A↔B 交替，当前仍是 A | B | 0.80 | 双人对话识别 |
| 8 | 以上都没有 | group | 0.20 | 默认 |

规则：

- 取最高分；同分按 1 > 2 > 3 > 6 > 4 > 5 > 7 > 8 的顺序取。
- 信号 1 与 6 可同时成立（`@bot @某人 你看这个`）——此时按 1 指向 bot，evidence 记两条。
- 名字匹配只认 `GROUP_PRESENCE_KEYWORDS`（当前 `大肥鱼`），避免把「雷霆」误当名字。

### 5.2 边界

- 纯图 / 纯表情：`text=""`，若同时 @bot 仍指向 bot；否则按 6/7/8。
- 引用目标解析不到：跳过信号 2，不猜测。
- 消息来自 bot 自己：不进入解析（`observe` 只记 ledger，不产生 Addressee）。

## 6. 参与策略（P2 已实现）

代码：`qq_bot/chat/participation.py`，在 judge 之前运行；只做「明确」判定，
模糊情况一律交回 presence（judge 或 0-1 抽奖）。

| 条件 | 动作 | reason |
|---|---|---|
| 没有会话上下文 | 交回 presence | `no_context` |
| `target == "bot"` 且 `score >= 0.90` | REPLY：跳过抽奖强制接（仍受静默/日上限约束） | `target_bot` |
| `target == "bot"` 且 `score < 0.90` | 交回 presence（交给 judge 判） | `bot_gray` |
| `target == 某用户` | PASS：不插嘴；窗口内累计一次 PASS | `target_other_user` |
| `target == "group"` 且话题相关 | 交回 presence（按 `RATE` 抽奖） | `topic_relevant` |
| `target == "group"` 且话题无关 | PASS | `topic_irrelevant` |
| 连续 PASS 达到 `PASS_LIMIT` | 关窗 SLEEP（复用 presence 的 `note_pass`） | `pass_limit` |

补充约定：

- 「你/你们」指向 bot 的 0.75 只作加分，不强制 REPLY（避免「你们」误伤）。
- PASS 不重置 `REPLY_COOLDOWN`，只累计 `PASS_LIMIT`（用户 m02430 拍板）。
- 强制接话时 `observe_active(force=True)` 跳过 `reply_cooldown` 与抽奖，
  但不跳过静默、黑名单、日上限、重复消息。
- `GROUP_PRESENCE_POLICY_ENABLED=false` 可整体退回 P3 行为（纯 judge/0-1）。

## 7. ~~Prompt 注入：`[群聊状态]`~~（v4 已移除）

> v4 决定：不再把聚合状态写进 prompt。归属改为在历史消息正文里逐条标
> `<user identity name="小明"/>`，见 §7.4。

### 7.1 格式（历史）

```
[群聊状态] 最近 10 分钟：隐身人×3（fw×3）、AMelax×2（废物×2）;
上一句是 小明 说的；本条指向：bot（依据：@bot）
```

- 最多列 5 个发言人，按发言数降序；
- 每人最多列 3 个高频文本；
- 没有可列内容时只输出指向与上一句；
- 目标 ≤ 80 tokens，超出则截断人数与词数。

### 7.2 注入位置

`qq_bot/chat/turn.py` 组装 system prompt 时，在群聊摘要之后、当前消息之前插入一段；仅群聊注入，私聊不注入。

### 7.3 配套规则（`prompts/chat/system.md`）

只加一句通用规则，不做场景分支：

> （v4 起不再注入；归属改由 §7.4 的逐条标签承担。）

### 7.4 逐条身份标签（v4 起）

`qq_bot/chat/turn.py` 的 `_annotate_history` 在拼 prompt 时给群历史里的
`role=user` 消息加前缀，**不落库**：

```
<user identity name="小明"/>
我钓了条三斤的
```

- `sender_name` 为空时用「某成员」；
- 已经带标签的旧数据不重复加；
- assistant/tool 消息不加；
- 与旧项目 `qq_bot` 的 `<user identity id=.../>` 同源，但只做渲染、不污染库。

## 8. 接入点

| 文件 | 改动 |
|---|---|
| `qq_bot/plugins/qqbot_official/__init__.py` | `_dispatch` 与非 @ 分支里对每条群消息调 `conversation.observe(...)` |
| `qq_bot/chat/turn.py` | 群聊拼 prompt 时给历史用户消息加 `<user identity>` 标签 |
| `qq_bot/services/config.py` | 7 个 `group_conversation_*` + `group_presence_policy_enabled` |
| 新文件 | `qq_bot/chat/conversation.py`、`qq_bot/chat/participation.py` |

## 9. 容量与生命周期

- 每群一个 `GroupConversation`，`MAX_GROUPS=512`，LRU 淘汰；
- 消息环形缓冲 30 条；账本按 10 分钟窗口统计；
- 进程内内存，不落库；重启后从空开始（P3 再从 `messages` 表重建）；
- 纯本地计算，不增加模型调用。

## 10. 参数表

| 变量 | 默认 | 说明 |
|---|---|---|
| `GROUP_CONVERSATION_ENABLED` | `true` | 总开关 |
| `GROUP_PRESENCE_POLICY_ENABLED` | `true` | 参与策略总开关（P2） |

## 11. 测试

### 11.1 单元测试

| # | 用例 |
|---|---|
| 1 | 同一用户 3 条 `fw` → `count_of(uid, "fw") == 3` |
| 2 | 两个用户各说各的 → 计数不合并 |
| 3 | 全角/大小写归一化后算同一条 |
| 4 | @bot → `target == "bot"`，score 1.0 |
| 5 | 文本「大肥鱼 在吗」→ CALL → bot |
| 6 | 上一句是 bot，「你觉得呢」→ bot（0.75） |
| 7 | @小明 → target 为小明，不是 bot |
| 8 | A↔B 交替 3 条 → 当前 A 指向 B |
| 9 | 无信号 → group 0.2 |
| 11 | 空会话 / 只有 bot 消息时不抛异常 |
| 12 | 超过 `MAX_GROUPS` 时最旧的群被淘汰 |
| 13 | 策略：指向 bot 1.0 → REPLY；0.75 → 交回 presence |
| 14 | 策略：指向某用户 → PASS；群里话题无关 → PASS |
| 15 | 窗口内连续 PASS 达到上限 → 关窗（复用 `note_pass`） |
| 16 | 强制接话：SLEEPING 也能唤醒，且跳过 `reply_cooldown` |

### 11.2 回放测试

用真实辱骂记录构造会话：

- 断言隐身人 `fw` = 3、AMelax `废物` = 2，且不合并；
- 断言第五条消息 `target == "bot"`；
- 断言账本计数与指向解析正确（v4 起不再有注入块）。

## 12. 分阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 | `conversation.py`、指向解析、测试（`[群聊状态]` 注入已于 v4 移除） | 已实现 |
| P2 | `ParticipationPolicy` 接管明确场景；回复目标按 Addressee 决定 @ 谁 | 已实现（策略部分；@ 选择未做） |
| P3 | 重启重建、指标、按群调参 | 未开始 |

## 13. 实现状态（P1 + P2）

- `qq_bot/chat/conversation.py`：`MessageNode` / `SpeakerLedger` / `Addressee` /
  `GroupConversation` / `ConversationManager`，含 `normalize_text`、
  `topic_tokens`、`parse_bot_names`。
- `qq_bot/chat/turn.py`：`_annotate_history` 给群历史用户消息加 `<user identity name="..."/>` 前缀（只渲染，不落库）。
- `qq_bot/plugins/qqbot_official/__init__.py`：@ 与非 @ 两条路径都调
  `conversation.manager.observe`；被动回复/主动接话成功后 `note_bot`。
- `qq_bot/chat/prompt.py`：`SystemPromptParts` 新增 `state` 字段（排在
  speaker 之后）。
- `qq_bot/services/config.py` 与 `.env.example`：7 个 `GROUP_CONVERSATION_*` 参数。
- `tests/test_conversation.py`：16 例（账本、归一化、8 条指向信号、渲染截断、
  LRU、辱骂场景回放）。
- `qq_bot/chat/participation.py`：`PolicyDecision` + `decide()`；
  接在 `autotrigger` 的 keyword 段之后、judge 之前。
- `qq_bot/chat/presence.py`：`policy_enabled`、`observe_active(policy_pass=...)`、
  `_decide_sleeping(force=...)` 的强制唤醒、force 跳过 `reply_cooldown`。
- `tests/test_participation.py`：6 例；`tests/test_presence.py` 追加 4 例（策略 PASS、
  强制唤醒、强制跳过冷却、@ 别人时不接）。

## 14. 风险与开放问题

1. **引用目标可获取性**：nonebot-adapter-qq 的引用信息是否稳定带 message_id，待实测；拿不到就跳过信号 2。
2. **名字匹配误伤**：「大肥鱼」出现在比喻/玩笑里会指向 bot；P1 先观察日志，P2 再决定是否加词性判断。
3. **注入成本**：每轮 +50–80 tokens，按当前 8.4k prompt 约占 1%，可接受。
4. **群名片更新**：改名后账本保留旧名，以 `last_at` 最新名字为准。
5. **策略阈值**：0.90 / 0.80 目前是经验值；先看 `presence policy` 日志的
   target/score 分布，再决定是否调。
6. **回复 @ 选择**：策略已算出 Addressee，但主动/被动回复还没按它选 @ 谁，
   P2 收尾或 P3 再做。
