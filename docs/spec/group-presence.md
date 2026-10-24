# 群聊存在感（Presence）与自动接话 · 设计规格

| 项 | 内容 |
|---|---|
| 版本 | v1.11（P1 + P2 已实现，实盘降噪） |
| 日期 | 2026-10-24 |
| 状态 | P1（存在感/抽奖/窗口/复读）与 P2（关键词）均已落地；按 10-24 评审校准：`RATE=0.2`、窗口内倍率 `2.0`、`DAILY_ACTIVE=120` |
| 替代 | `qq_bot/chat/autotrigger.py` 的 L1 自动接话、`qq_bot/chat/activation.py` 的打分判定 |
| 前提 | QQ 官方机器人，个人认证；频控以官方文档为准 |

---

## 1. 目标与非目标

### 1.1 目标

- 从「被 @ 才可能接话」改成「按群活跃度概率出场，出场后在一段时间内像在场的人一样接话」。
- 所有频率参数可调、可解释、可观测、可急停。
- 行为边界明确：不刷屏、不答非所问、被要求闭嘴时真的闭嘴。
- 有一个纯代码的「复读机」：同一句话被多人短时间重复时，跟着复读一次。

### 1.2 非目标

- 不做语义相关性打分。`min_score` 规则引擎退役。
- 本规格不实现「关键词 + 语义判定」，只留接口与约束（Phase 2，见 §6.9）。
- 不改 @ 被动回复的既有链路与 `passive_window` 记账。

---

## 2. 平台约束（官方文档）

| 项 | 限制 |
|---|---|
| 被动消息 | 有效期 5 分钟，每条消息最多回复 5 次 |
| 主动消息 · Bot 维度 | 企业认证 / 个人身份证认证 60 条/分钟；未认证 30 条/分钟 |
| 主动消息 · 单群维度 | 20 条/分钟；每群每天最多接收 1000 条 |
| 群消息 | 不支持流式参数 |

结论：

- **配额不是瓶颈**。单群 1000 条/天、20 条/分钟，正常接话量远低于上限。
- 真正的约束是**行为**：不能刷屏、不能答非所问、不能在被要求闭嘴时说话。
- 每日上限保留，但定位从「额度限制」改为「行为熔断」，默认值见 §7。

---

## 3. 术语

| 术语 | 含义 |
|---|---|
| 合格消息 | 通过 §6.6 可接性过滤的群消息 |
| 抽奖 | 每条合格消息按 `RATE` 做一次独立伯努利试验 |
| 窗口 / 在场 | 抽中后进入的 `WINDOW_SEC` 区间，期间 bot 被认为「在群里」 |
| 发言冷却 | bot 说完一句后，`REPLY_COOLDOWN_SEC` 内不再抽奖 |
| 唤醒冷却 | 窗口关闭后，`WAKE_COOLDOWN_SEC` 内不再抽奖 |
| 静默 | 被要求闭嘴后的 `MUTE_SEC`，期间禁止一切主动行为 |
| ack | acknowledgement，回执；静默时先回的那句「好」，只表示指令收到 |
| 复读机 | 同一文本被多人短时间重复时，bot 跟着发一次；纯代码，不走 LLM |
| 主动 / 被动 | 主动 = 不带 msg_id 的 `send_to_group`，花额度；被动 = 带 msg_id 的回复，5 分钟 / 5 次 |

---

## 4. 状态机

### 4.1 状态

| 状态 | 含义 | 携带数据 |
|---|---|---|
| `SLEEPING` | 不在场 | `wake_cooldown_until` |
| `AWAKE` | 在场 | `expires_at`、`last_spoke_at` |
| `MUTED` | 静默 | `until`、`trigger_message_id` |

> 被动回复不是状态，是通道能力。任何状态都可以被动回复（`MUTED` 下只有被动回复可用）。
>
> 复读机也不改变状态：它只更新 `last_spoke_at` 与日计数。

### 4.2 转移表

| 从 | 事件 | 到 | 条件 |
|---|---|---|---|
| `SLEEPING` | 合格消息 + 抽中 | `AWAKE` | 唤醒冷却已过、日上限未满、该群有主动能力 |
| `SLEEPING` | 合格消息 + 未中 | `SLEEPING` | — |
| `AWAKE` | 合格消息 + 抽中 | `AWAKE` | 发言冷却已过、窗口条数未用尽 |
| `AWAKE` | 窗口到期 | `SLEEPING` | 进入唤醒冷却 |
| `AWAKE` | 日上限 / 窗口条数用尽 | `SLEEPING` | 进入唤醒冷却 |
| `AWAKE` | 主动发送失败（权限/限流/违规） | `SLEEPING` | 见 §10 |
| `任意` | @bot 且命中静默词 | `MUTED` | 先回执，再置静默（触发消息的回复不算解除） |
| `任意` | 新静默命令（已在 `MUTED`） | `MUTED` | 重置 30 分钟 |
| `MUTED` | 被动回复（非触发消息） | `SLEEPING` | 立即解除，回到不在场，不开窗 |
| `MUTED` | 到期 | `SLEEPING` | — |

### 4.3 状态图

```
                抽中 & 冷却过 & 额度有
   ┌─────────┐ ───────────────────────► ┌─────────┐
   │SLEEPING │                          │  AWAKE  │
   └─────────┘ ◄─────────────────────── └─────────┘
        ▲         到期/上限/发送失败          │
        │                                     │
        │  @bot 命中静默词                    │ 静默词
        │  ┌─────────┐                        │
        └─ │  MUTED  │ ◄──────────────────────┘
           └─────────┘
              │  被动回复（非触发消息）/ 到期
              └──────────────► SLEEPING
```

---

## 5. 消息判定管线

每条群消息按以下顺序处理，命中即返回：

```python
def on_group_message(msg, now):
    # 0. 硬过滤（不改状态）
    if msg.author.bot:            return   # 官方机器人 / 自己
    if is_command(msg.text):      return   # "/" 开头的命令

    # 1. @ 路径：被动回复，不受静默/黑名单/概率影响
    if msg.mentions_me:
        if matches_mute_words(msg.text):
            reply_passive(mute_ack)                 # 先回执
            enter_mute(now, trigger_id=msg.id)      # 再静默
            return
        reply_passive(turn)                         # 正常问答
        if state is MUTED and msg.id != state.trigger_id:
            exit_mute()                             # @ 解除静默，回 SLEEPING
        return

    # 2. 静默 / 黑名单
    if state is MUTED:                      return
    if msg.author_openid in ignore_list:    return

    # 3. 复读机：纯代码，不经过 LLM，不改变在场状态
    if repeat_enabled and is_repeatable(msg) and repeat.hit(msg, now):
        speak_repeat(msg, now)
        return

    # 4. 可接性过滤
    if not acceptable(msg):                 return

    # 5. 在场：先检查冷却与上限，再抽奖
    if state is AWAKE:
        if now >= state.expires_at:
            close_window("timeout", now);   return
        if now < state.last_spoke_at + reply_cooldown:  return
        if daily_count >= daily_active:
            close_window("daily_cap", now); return
        if rand() < rate * rate_in_window:
            speak(msg, now)
        return

    # 6. 不在场：抽奖 -> 唤醒 -> 接这一条
    if now < state.wake_cooldown_until:     return
    if daily_count >= daily_active:         return
    if not can_send_proactive(group):       return
    if rand() < rate:
        wake(now)
        speak(msg, now)                     # 抽中即接这条
```

要点：

- 冷却结束 ≠ 立刻发言。冷却只恢复抽奖资格，是否说话仍由 `RATE` 决定。
- 不合格消息不抽奖，避免「中了但接不了」的空奖。
- 窗口到期先关窗、进入唤醒冷却，再丢弃当前消息，不立即重抽。
- 复读机与 0-1 相互独立：复读成功只计入日计数，**不占用发言冷却**（不写 `last_spoke_at`），也不改变 `SLEEPING/AWAKE`。

---

## 6. 机制细则

### 6.1 0-1 抽奖与活跃度耦合

- 每条合格消息独立伯努利，`p = RATE`；窗口内乘以 `RATE_IN_WINDOW`。
- `RATE` 的语义是「一个人随手瞄一眼群」的随机性，不是「想说话」的概率；窗口内「读消息」概率接近 1，接不接话由 `RATE_IN_WINDOW` 表达。
- 与群活跃度天然耦合：单位时间期望触发次数 ≈ `λ · p`（λ = 条/分钟）。
- 用 `WAKE_COOLDOWN` 给「出场密度」封顶：窗口关闭后必须先冷却，才能再次唤醒。
- 窗口占空比上限 ≈ `WINDOW / (WINDOW + WAKE_COOLDOWN)`。

| λ（条/分） | RATE | 平均多久抽中一次 | 观感 |
|---|---|---|---|
| 0.5（5 人冷清群） | 0.2 | ~10 分钟 | 每 10 分钟左右「瞄一眼群」，占空比约 40% |
| 2（5 人正常群） | 0.2 | ~2.5 分钟 | 醒来后基本一直在场，由冷却压到 67% 占空比 |
| 10（热闹群） | 0.2 | ~0.5 分钟 | 窗口几乎常开，靠唤醒冷却封顶 |

- 窗口内 `p = 0.2 × 2.0 = 0.4`：对话里大约回 40% 的消息，10 条消息接 4 条；**不设条数上限**，靠 `REPLY_COOLDOWN` 和日上限兜底——效果是「每次醒来参与一场对话」，不是「句句接」。
- 5 人小群的 λ 通常只有 0.2–2 条/分，所以 0.2 的实际观感是「几分钟到十几分钟瞄一眼」，不会瞬间刷屏。

> 数值需实盘校准。日上限见 §7，先按「行为熔断」设，不按配额设。

### 6.2 窗口

- 长度 `WINDOW_SEC`，默认 600 秒，env 可调。
- **固定到期，不因发言续期**：保证一定会退场，避免「聊得越多待得越久」。
- 关闭时记录 `reason`：`timeout` / `daily_cap` / `mute` / `send_error`。
- 关闭后设置 `wake_cooldown_until = now + WAKE_COOLDOWN`。
- **没有发言条数上限**（10-24 确认删掉 `WINDOW_REPLIES`）：节奏完全由 `RATE × RATE_IN_WINDOW` 和 `REPLY_COOLDOWN` 决定。窗口内每条合格消息独立抽奖，抽中就接，只受 15 秒冷却与日上限约束。
- 极端活跃时（λ≥3 条/分）单个窗口可能接十几条；届时调大 `REPLY_COOLDOWN` 或降低 `RATE_IN_WINDOW`，不再恢复条数上限。

### 6.3 双冷却

| 冷却 | 触发 | 作用 | 默认 |
|---|---|---|---|
| `REPLY_COOLDOWN` | bot 刚说完一句 | 说了一句后别马上再说下一句 | 15 秒，可设 0 |
| `WAKE_COOLDOWN` | 窗口关闭 | 退场后别马上又醒 | 300 秒 |

- 两者独立，不共享。
- 已确认维持：`REPLY_COOLDOWN` 默认 15 秒（比旧实现的 90 秒短，避免话题内互动时话题过期），激进可设 0；此时由 `RATE`、`RATE_IN_WINDOW` 和日上限兜底。
- 设 0 的风险：同一用户连发多条（「在吗」「在吗」「在吗」）会连续被接，`RATE=1` 时尤其明显。

### 6.4 静默

- 触发：**@bot** 且文本命中 `MUTE_WORDS`（自然语言，默认「闭嘴 / 别说了 / 安静」）。任何人都可以触发。
- 行为：先回一句 `MUTE_ACK`（默认「好」；ack = acknowledgement，回执，只表示指令收到），再置 `MUTED` 30 分钟。
- 解除：
  1. 提前解除：任意后续被动回复（即有人 @bot 说别的内容）；
  2. 到期解除；
  3. 重启失效（内存态，已确认可接受）。
- 关键边界：**触发静默那条消息的 ack 回复不算解除**。实现上用 `trigger_message_id` 排除，否则会「刚闭嘴就解除」。
- 静默期间：只允许 @ 被动回复；0-1、关键词、主动消息全部禁止。
- 静默期间再次触发：重置 30 分钟（可配置为不延长）。
- 触发词维持三个（10-24 确认）：`闭嘴` / `别说了` / `安静`，暂不扩「别吵了」「消停会」。

### 6.5 黑名单

- 维度：`openid`（群里可能有其他机器人或不想被接话的人）。
- 配置：`IGNORE_OPENIDS` env 作种子；管理命令 `/ignore <openid>`、`/unignore <openid>`、`/ignorelist`（仅管理员）。
- 范围：只挡**主动**路径（0-1 与关键词），不挡 @ 被动回复。
- 消息仍照常落库，只是不触发接话。
- 平台标记的机器人账号由 `author.bot` 过滤（`qq_bot/plugins/qqbot_official/__init__.py:158`），不进黑名单逻辑。

### 6.6 可接性过滤（Acceptability）

**定义**：判断一条消息「适不适合作为接话对象」，只看形式和语境，**不做语义相关性判断**。它是去掉 `min_score` 之后的安全网，不是打分器。

| 规则 | 处理 | 理由 |
|---|---|---|
| 命令（`/` 开头） | 不接 | 走命令链路 |
| @ 了别人 | 不接 | 人家在跟别人说话 |
| 纯链接 | 不接 | 没有可接的内容 |
| 纯表情包（face/mface） | 不接 | 含义太抽象，不处理 |
| 纯图片 | **接** | 模型有视觉能力（`qq_bot/ai/openai_client.py:56` 会把图片转成 image_url parts；`qq_bot/chat/turn.py:206` 会下载图片），可结合群聊记录评价/回复 |
| 短文本（「难绷」「闹麻」「扫比」） | **接** | 不做长度过滤；短句可能是有效情绪表达，交给模型判断 |
| 重复推送（同 message_id） | 不接 | 幂等 |

- 不再采用长度阈值（v1 草案里的 `MIN_CHARS`）：长度不能代表信息量，「难绷」这类抽象情绪正是群聊语料。
- 纯图片会触发 0-1，turn 链路会下载图片并走 vision 调用，**成本高于纯文本**；因此纯图片的抽奖概率乘以 `IMAGE_RATE_MULT`（默认 0.5），可在 env 调整。
- 不合格消息**不抽奖**，也不消耗任何冷却。
- 复读机有自己独立的判定（§6.7），不复用这里的「接」集（例如它允许 @ 他人）。

### 6.7 复读机（纯代码，不走 LLM）

**目的**：群里同一句话被多人短时间重复时，bot 严格原样跟着复读一次，制造「在场」感，不花模型成本。

触发条件（全部满足）：

| 条件 | 默认 |
|---|---|
| 消息只含文本段与 @ 段（图片、表情、卡片不参与） | — |
| 文本非空、非命令、非纯链接 | — |
| 未 @ bot 本人、未 @ 全体成员 | — |
| 文本长度 ≤ `REPEAT_MAX_CHARS` | 20 |
| 「@ 目标序列 + 归一化文本」在 `REPEAT_WINDOW_SEC` 内被至少 `REPEAT_MIN_USERS` 个不同用户发送（不含 bot、不含黑名单） | 3 人 / 90 秒 |

匹配键与复读规则：

- **计数按不同用户去重**：同一用户在同一波只算一票；同一用户连续刷同一句**不会**触发复读。
- 键 = `(被 @ 用户的 openid 序列, 归一化文本)`。归一化只用于**比较**：去首尾空白、去尾部标点、全角转半角。
- **发送严格原样**：按触发消息的原始段结构重建——文本段逐字照抄，@ 段用 `mention_user(openid)` 重新构造。@ 了谁就 @ 谁，不吞、不改、不加语气、不换标点。
- 只有 @ 目标与文本都一致才算同一句：「@张三 难绷」和「@李四 难绷」不是同一句。
- 同一波只复读一次：同一 key 复读后，`REPEAT_WINDOW_SEC` 内不再重复（去重，不是限频）；窗口过后再出现可再次触发。

动作：

- 直接发送重建后的消息，不加前后缀。
- **不做单独限频**：命中即复读。只受两个全局闸门约束——`MUTED`、群日上限。
- 不进入窗口、不改变 `SLEEPING/AWAKE`；不写 `last_spoke_at`、**不占用 `REPLY_COOLDOWN`**；计入日上限。
- 不检查也不写入 `REPLY_COOLDOWN`：复读与 0-1 的发言冷却完全解耦。
- 走**主动消息**（已定；单群 1000 条/天，额度足够）。若后续实测非 @ 消息的 msg_id 可以被动回复，再讨论是否切被动省额度。
- 失败处理见 §10。

为什么值得做：

- 零模型成本、响应快，适合「短平快」的群聊节奏。
- 复读是强参与信号，比硬接一句更自然；带 @ 的复读更像真人跟梗。
- 与 0-1 独立：`RATE` 很低时复读仍可能发生，反之亦然。

风险与边界：

- 严格原样意味着可能复读敏感内容，平台会以 `40034006` 拒绝；此时不重试，也不做本地改写（改写就破坏「原样」了）。
- 不限频 → 同一波可能连续被复读；这是预期效果，靠 `MUTED` 和日上限兜底。
- 主动消息里 @ 用户可能受平台限制，需实测；失败按 §10 处理。
- @ 复读会真的提醒被 @ 的人，深夜刷屏风险由 `MUTED` / 调高 `REPEAT_MIN_USERS` 控制。

### 6.8 @ 被动回复

- 链路不变：`bot.send(event, seg)`，带 msg_id，5 分钟 / 5 次。
- @ 不参与 0-1，不开启窗口（按用户决定）。
- @ 在静默中也始终可用；它同时是静默的解除方式。
- @ **不关闭已经开启的 `AWAKE` 窗口**（10-24 确认）：@ 不负责开窗，也不负责关窗；答完继续在场，直到窗口自然到期。
- 被动额度仍由 `passive_window.py` 记账，窗口登记时保守记已用 4 段。

### 6.9 关键词触发（Phase 2）

**关键词**：bot 名字「大肥鱼」，精确子串匹配；不做模糊、拼音、分词。旧别名「肥鱼」「鱼鱼」不启用。

**为什么要先分类**：同一串名字，可能是在叫 bot，也可能是在聊 bot，或者只是同名（鱼、菜、宠物）。三种语境三种反应，不能一刀切。

| 语境 | 判定信号 | 行为 |
|---|---|---|
| `CALL` 在叫 bot | 名字独立成句/位于句首；后跟「在吗/在不在/出来/问你/帮我」；同句出现「你」；引用或回复的是 bot 的消息；整条消息只有名字 | 正常 turn 回答一次；不进窗口（避免叫一声就把 bot 留在场上） |
| `ABOUT` 在聊 bot | 名字作第三人称主语/宾语；「大肥鱼怎么样/今天没说话/真菜」；没有同名信号 | 过渡句 + 邀请加入；进入窗口；开回钩 |
| `HOMONYM` 不是 bot | 「我的/我家的/这条/买了/好吃/钓」；鱼、菜、宠物、游戏角色语境；@ 的是人类「大肥鱼」 | 默认不触发；可配置为「装傻加入」 |

判定：规则快路优先，剩余歧义交 flash 分类（输出 `CALL|ABOUT|HOMONYM`，`max_tokens=8`、`temperature=0`、2 秒超时；`fail-closed` 到 `HOMONYM`，宁可不动）。只在名字出现时调用，量很小。

**过渡句**（`ABOUT` 专用，模型生成）：交给 flash 按人设写，不硬编码文案。

输入：

- 人设片段：`prompts/chat/persona.md`、`social.md`、`style.md`（与主对话同一套）；
- 群摘要：`SystemPromptParts.summary`（由 `qq_bot/services/memory_compress.py` 产出）；
- 最近群消息：`qq_bot/memory/assembler.py` 的 `group_window` 最近 `KEYWORD_CONTEXT_MESSAGES` 条（默认 15）；
- 触发消息原文 + 发送者昵称；
- 指令：先对「被提到」做一句符合语境/情绪的反应（不要硬夸），再邀请加入；合计 ≤2 句、≤60 字。

**明确不包含**：`thread_window`（用户个人线程的聊天记录）与个人长期记忆。过渡只读群内公开上下文。

约束：

- 1–2 句，不 @ 人、不带「回复：」前缀、不输出引号包裹；
- `temperature=0.8` 保留人味，超时 2.5 秒；
- 失败/超时回退兜底句（`GROUP_PRESENCE_KEYWORD_FALLBACK`）：**「什么意思」**，不加任何其他内容。
- 触发消息按原文包进 `<message>` 标签，指令放 system，降低注入风险；
- 生成结果走正常分段 / 打字停顿发送。

- `{nick}` 取发送者群名片/昵称；取不到时用「有人」，不要用「你」。

**回钩（hook）**：过渡句是提问，必须保证有人接话时接得住。

- 过渡发出后 `HOOK_SEC`（默认 120 秒）内，第一条合格的、来自非黑名单用户的消息按 `HOOK_RATE`（默认 1.0）被接；
- 接完清除回钩，回到常规窗口逻辑；
- 120 秒内没人接，回钩自然过期。

**窗口**：只有 `ABOUT` 进入 `AWAKE`（`KEYWORD_WINDOW_SEC`，默认 300 秒），并开回钩；`CALL` 只回答一次，状态保持 `SLEEPING`，后续消息仍按 0-1 抽奖与冷却走。

**额外闸门**：`KEYWORD_COOLDOWN_SEC`（群级，默认 120 秒）与 `KEYWORD_USER_COOLDOWN_SEC`（用户级，默认 300 秒），防止刷名字钓鱼；静默、日上限照旧，关键词不绕过任何一条。**黑名单用户的消息不触发关键词**（与 0-1 一致），但其 @bot 仍走被动回复。

**不触发**：引用/转发内容里的名字（只匹配本条文本段）；图片/表情/卡片里的名字；@ 了别人的消息；命令；bot 自己的消息。

**与 0-1 的关系**：关键词优先于 0-1 抽奖，命中即走关键词路径，不再抽奖。窗口内再次出现名字，不重复开窗、不重复过渡，避免打断。

**与 @ 被动的关系**：@bot 走被动通道（免费），在管线最前面处理；关键词 `CALL` 是主动消息（花额度），两者不冲突。

边界情景：

| 情景 | 行为 |
|---|---|
| 单喊「大肥鱼」 | `CALL`，回「在」或「嗯？」 |
| 「大肥鱼 在吗」 | `CALL`，正常回答 |
| 「大肥鱼帮我查下天气」 | `CALL`，走工具链 |
| 「大肥鱼真菜」 | `ABOUT`，负面分支 |
| 「我觉得我的大肥鱼更牛逼」 | `ABOUT`，模型生成过渡 + 回钩 |
| 「@张三 大肥鱼今天没来」 | 不触发（@ 别人） |
| 群友昵称也叫「大肥鱼」 | `HOMONYM`，默认跳过 |
| 「大肥鱼丸好吃吗」 | `HOMONYM`，默认跳过 |
| 刷屏喊名字 | 群级/用户级冷却压住；同一用户连刷不额外触发 |
| 窗口内再提名字 | 不重复开窗 |
| 深夜 | 不做免打扰，照常触发 |

---

## 7. 参数表

| ENV | 默认 | 范围 | 说明 |
|---|---|---|---|
| `GROUP_PRESENCE_ENABLED` | `true` | bool | 总开关 |
| `GROUP_PRESENCE_RATE` | `0.2` | 0.0–1.0 | 每条合格消息的抽奖概率；0 = 永不出场，1 = 必中（仍受冷却与上限约束） |
| `GROUP_PRESENCE_WINDOW_SEC` | `600` | 60–3600 | 在场窗口长度 |
| `GROUP_PRESENCE_RATE_IN_WINDOW` | `2.0` | 0.1–5.0 | 窗口内概率倍率；窗口内 `p = 0.2 × 2.0 = 0.4` |
| `GROUP_PRESENCE_REPLY_COOLDOWN_SEC` | `15` | 0–600 | 发言后冷却；0 = 关闭 |
| `GROUP_PRESENCE_WAKE_COOLDOWN_SEC` | `300` | 0–3600 | 窗口关闭后的抽奖冷却 |
| `GROUP_PRESENCE_DAILY_ACTIVE` | `120` | 0–1000 | 每群每天主动条数上限（平台 1000/天，此值为行为熔断） |
| `GROUP_PRESENCE_MUTE_SEC` | `1800` | 60–86400 | 静默时长 |
| `GROUP_PRESENCE_MUTE_WORDS` | `闭嘴,别说了,安静` | list | 静默触发词 |
| `GROUP_PRESENCE_MUTE_ACK` | `好` | str | 静默回执 |
| `GROUP_PRESENCE_IGNORE_OPENIDS` | 空 | list | 黑名单种子 |
| `GROUP_PRESENCE_IMAGE_RATE_MULT` | `0.5` | 0.0–1.0 | 纯图片消息的抽奖概率倍率（vision 成本更高） |
| `GROUP_PRESENCE_REPEAT_ENABLED` | `true` | bool | 复读机开关 |
| `GROUP_PRESENCE_REPEAT_MIN_USERS` | `3` | 2–10 | 触发所需的不同用户数 |
| `GROUP_PRESENCE_REPEAT_WINDOW_SEC` | `90` | 10–600 | 统计窗口与同波去重窗口 |
| `GROUP_PRESENCE_REPEAT_MAX_CHARS` | `20` | 1–50 | 可复读文本最大长度 |
| `GROUP_PRESENCE_KEYWORDS` | `大肥鱼` | list | 精确匹配；只启用全名，旧别名不启用 |
| `GROUP_PRESENCE_KEYWORD_COOLDOWN_SEC` | `120` | 0–3600 | 群级关键词冷却（防刷名字） |
| `GROUP_PRESENCE_KEYWORD_USER_COOLDOWN_SEC` | `300` | 0–3600 | 用户级关键词冷却 |
| `GROUP_PRESENCE_KEYWORD_WINDOW_SEC` | `300` | 60–3600 | `ABOUT` 语境的窗口长度 |
| `GROUP_PRESENCE_HOOK_SEC` | `120` | 0–600 | 过渡句之后的回钩有效期 |
| `GROUP_PRESENCE_HOOK_RATE` | `1.0` | 0.0–1.0 | 回钩期内接话概率 |
| `GROUP_PRESENCE_KEYWORD_HOMONYM_MODE` | `skip` | skip/playful | 同名异物：跳过 or 装傻加入 |
| `GROUP_PRESENCE_KEYWORD_CONTEXT_MESSAGES` | `15` | 5–100 | 过渡句参考的最近群消息条数 |
| `GROUP_PRESENCE_KEYWORD_FALLBACK` | `什么意思` | str | 模型超时/失败时的兜底过渡句，不加任何其他内容 |

旧配置映射（保留一个版本兼容）：

| 旧字段 | 新字段 | 说明 |
|---|---|---|
| `group_autotrigger` | `GROUP_PRESENCE_ENABLED` | 语义不变 |
| `group_autotrigger_probability` | `GROUP_PRESENCE_RATE` | 概率语义从「打分后概率」变为「每条消息抽奖」 |
| `group_autotrigger_window_seconds` | `GROUP_PRESENCE_WINDOW_SEC` | 旧值 300 是被动窗口，新值默认 600，不要直接沿用心智 |
| `group_autotrigger_cooldown_sec` | `GROUP_PRESENCE_REPLY_COOLDOWN_SEC` | 从 90 缩短到 15 |
| `group_autotrigger_daily_cap` | `GROUP_PRESENCE_DAILY_ACTIVE` | 从 6 提高到 120 |
| `group_autotrigger_min_score` / `names` / `hint` | 移除 | 打分器退役 |

---

## 8. 数据与持久化

| 数据 | 存储 | 重启后 |
|---|---|---|
| 状态机（SLEEPING/AWAKE/MUTED） | 内存 | 清零（已确认可接受） |
| 冷却时间戳、窗口条数 | 内存 | 清零 |
| 日计数 | 内存（按本地日期 key） | 清零；跨天自动重置 |
| 黑名单 | DB 表 / JSON 文件 | 保留 |
| 静默 | 内存 | 清零（已确认可接受） |

---

## 9. 日志与观测

每次决策输出一行结构化日志：

```
presence decide group=... state=SLEEPING rate=0.08 roll=0.031 outcome=wake
presence speak  group=... msg=... parts=2 latency=1.2s daily=3/60
presence exit   group=... reason=timeout duration=600s replies=2
presence mute   group=... by=<openid> trigger=<message_id> until=...
presence unmute group=... reason=passive
presence repeat group=... text="难绷" mentions=1 users=3 latency=1.2s daily=4/60
presence skip   group=... reason=ignore|not_acceptable|cooldown|daily_cap|rate|repeat_burst
presence error  group=... code=40034105 action=disable_proactive
```

每周能从日志回答的问题：唤醒次数、窗口占空比、主动条数、失败率、被静默次数。

---

## 10. 失败处理与熔断

| 错误 | 处理 |
|---|---|
| `40034105` 主动消息无权限 | 该群 `proactive_ok=false`，立即关窗，进入冷却；每小时探测一次 `get_group_bot_state` |
| `40034006` 内容违规 | 不重试，关窗，记录原文 |
| `429` / 频控 | 关窗，全局 60 秒冷却 |
| 网络异常 | 关窗，30 秒冷却 |
| 复读发送失败 | 不改窗口状态；不重试；连续失败按上表熔断 |
| 同群连续 2 次失败 | 当天该群关闭主动路径，只保留被动 |

---

## 11. 测试矩阵

| # | 场景 | 期望 |
|---|---|---|
| 1 | RATE=0 | 永不唤醒，@ 仍正常 |
| 2 | RATE=1 + 冷却 0 | 每条合格消息都被接，直到日上限 |
| 3 | 唤醒冷却内 | 不抽奖 |
| 4 | 窗口到期 | 回到 SLEEPING，设置唤醒冷却 |
| 5 | 日上限用尽 | 关窗，当天不再主动 |
| 6 | 不合格消息（纯表情包/命令/@别人/纯链接） | 不抽奖、不消耗冷却 |
| 7 | 重复推送同一 message_id | 只处理一次 |
| 8 | @bot「闭嘴」 | 先 ack，再 MUTED；ack 不解除 |
| 9 | MUTED 中 @bot 说别的 | 被动回复 + 解除静默 |
| 10 | MUTED 到期 | 回 SLEEPING |
| 11 | MUTED 中收到普通消息 | 不抽奖、不复读 |
| 12 | 黑名单用户发消息 | 不抽奖；@bot 仍回复；不计入复读 |
| 13 | 发送 40034105 | 关窗、该群主动能力置 false |
| 14 | 发送 40034006 | 不重试、关窗 |
| 15 | 窗口内命中静默词 | 立即关窗 + MUTED |
| 16 | 重启 | 静默清零，状态回 SLEEPING |
| 17 | 纯图片消息 | 可接、参与抽奖，概率 × `IMAGE_RATE_MULT` |
| 18 | 短句「难绷」 | 可接、参与抽奖（不做长度过滤） |
| 19 | A/B/C 三人 90 秒内各发「难绷」 | bot 复读一次「难绷」 |
| 20 | 三人各发「@张三 难绷」 | 复读一次，且带 @张三 |
| 21 | 「@张三 难绷」×3 与「@李四 难绷」×2 | 只有前者触发；@ 目标不同不算同一句 |
| 22 | 同一波第 4、5 人也发同一句 | 不再重复（同波去重） |
| 23 | 消息 @bot 或 @全体 | 不参与复读 |
| 24 | 消息含图片/表情 | 不参与复读 |
| 25 | RATE=0、复读开启 | 不抽奖，但复读仍可触发 |
| 26 | 复读发送 40034006 | 不重试、状态不变 |
| 27 | 单发「大肥鱼」 | `CALL`，回一次「在」，不开窗 |
| 28 | 「大肥鱼 在吗」 | `CALL`，正常回答 |
| 29 | 「我觉得我的大肥鱼更牛逼」 | `ABOUT`，过渡句带昵称 + 邀请，开回钩 |
| 30 | 过渡后 120 秒内有人回「在聊原神」 | 回钩命中，正常接话 |
| 31 | 「@张三 大肥鱼今天没来」 | 不触发 |
| 32 | 「大肥鱼丸好吃吗」 | `HOMONYM`，默认不触发 |
| 33 | 120 秒内连刷「大肥鱼」 | 群级冷却，只触发一次 |
| 34 | 静默中提名字 | 不触发 |
| 35 | 同一用户连发 10 条「难绷」 | 不复读（只算一票）；关键词走用户级冷却 |
| 36 | 过渡句模型超时 | 回退兜底句，仍开窗口 + 回钩 |
| 37 | 发送者昵称取不到 | 过渡句用「有人」 |

测试要求：时钟可注入（`now` 参数）、随机数可注入（`rand` 参数），不依赖真实 sleep。

---

## 12. 与现有代码的迁移

| 现有 | 处理 |
|---|---|
| `qq_bot/chat/autotrigger.py` | 改为薄适配层，或由 `presence.py` 取代；`maybe_autotrigger` 保留为入口名 |
| `qq_bot/chat/activation.py` | 退役（打分、veto、实词重叠全部移除） |
| `qq_bot/chat/passive_window.py` | 保留，继续为 @ 路径记账 |
| `qq_bot/chat/session_lock.py` | 保留，串行发送不变 |
| `qq_bot/plugins/qqbot_official/__init__.py` | `handle_full_message` 改调 `presence.on_group_message`；`_dispatch` 的 @ 链路不变 |
| `qq_bot/services/config.py` | 新增 presence 字段，旧字段标 deprecated |
| `tests/test_autotrigger.py` | 替换为 `tests/test_presence.py`（按 §11 矩阵） |

新增模块建议：

- `qq_bot/chat/presence.py`：状态机、抽奖、冷却、静默、管线。
- `qq_bot/chat/repeat.py`（或并入 `presence.py`）：复读检测与发送。
- `qq_bot/chat/presence_store.py`（可选）：黑名单与日计数。
- `qq_bot/chat/relevance.py`：Phase 2。

---

## 13. 分期

| 阶段 | 内容 |
|---|---|
| P1 | 状态机 + 抽奖 + 双冷却 + 窗口 + 静默 + 黑名单 + 可接性过滤 + 复读机 + 日志 + 测试 |
| P2 | 关键词触发：`CALL/ABOUT/HOMONYM` 分类、过渡句、回钩（`relevance.py`） |
| P3 | 参数校准、观测面板、按群覆盖参数 |

---

## 13.5 实现状态（P1 + P2，2026-10-24）

| 文件 | 状态 |
|---|---|
| `qq_bot/chat/presence.py` | 新增：状态机、抽奖、窗口、双冷却、静默、黑名单、可接性、复读检测、关键词开窗、回钩、失败熔断 |
| `qq_bot/chat/relevance.py` | 新增（P2）：关键词三语境判定（规则快路 + flash 兜底）、过渡句生成与清洗 |
| `qq_bot/chat/autotrigger.py` | 改为薄适配层：`maybe_autotrigger` 入口不变，负责转事件、关键词分流、发送、记账 |
| `qq_bot/chat/activation.py` | 删除（打分/veto/实词重叠全部移除） |
| `qq_bot/chat/passive_window.py` | 保留，@ 路径继续登记（P1 的主动消息不再从它取通道） |
| `prompts/chat/keyword_transition.md` | 新增（P2）：过渡句指令模板 |
| `qq_bot/plugins/qqbot_official/__init__.py` | `_dispatch` 接入静默词/解除；`handle_full_message` 改调新编排层 |
| `qq_bot/services/config.py` | `group_autotrigger_*` 换成 `group_presence_*`；P2 追加 keyword/hook 字段 |
| `.env.example` / `.env` | 同步新变量名 |
| `tests/test_presence.py` | 新增 47 例（P1 + P2） |
| `tests/test_relevance.py` | 新增（P2）9 例 |

实现细节（与文档的对应关系）：

- 抽奖在 `observe_active`：可接性 → 回钩 → 复读 → `AWAKE` 分支（`p = RATE × RATE_IN_WINDOW`）或 `SLEEPING` 分支（`p = RATE`）；纯图再乘 `IMAGE_RATE_MULT`。
- 复读键 = `(被 @ openid 序列, 归一化文本)`；发送按原消息段重建（`MessageSegment.mention_user`），不写 `last_spoke_at`，只计入日上限；同一波 90 秒去重。
- 静默：`observe_passive` 命中词 → 返回 `MUTE_ACK`，插件只发回执、不再跑 turn；触发那条消息的 ack 用 `mute_trigger_id` 排除，不会秒解除。
- 关键词：`find_keyword` 命中后走 `fast_kind` 规则快路，歧义才调 `deepseek-v4-flash`（`max_tokens=8`，2 秒超时，fail-closed 到 `HOMONYM`）；`CALL` 直接跑 turn（不加 `AUTO_HINT`），只回一次、不开窗（状态保持 `SLEEPING`）；`ABOUT` 生成过渡句并主动发出，开 `KEYWORD_WINDOW` 与 `HOOK`；`HOMONYM` 跳过，不抽奖。
- 回钩：`ABOUT` 后 `HOOK_SEC` 内第一条可接消息直接回复（`HOOK_RATE` 默认 1.0），只用一次；`ABOUT` 的 0-1 抽奖在回钩之后才走。
- 失败：`40034105` → 该群 `active_ok=False`；`40034006`/`429` → 关窗并冷却；网络错误连续 2 次 → 当天关闭主动。

群聊发言规则：`prompts/chat/group.md` 增加「先判断在跟谁说」的插话规则（不抢答、只接一句、拿不准就问），并随 `get_group_prompt()` 进入每次 turn 的 system prompt。

已知偏差：

1. 复读的「同波 90 秒去重」按 §6.7 保留，不是限频，但实际效果是同一句话 90 秒内最多复读一次。
2. `note_failure` 的冷却秒数是实现里写死的（300/60/30），未做成 env；等实盘再参数化。
3. 关键词判定默认模型是 `deepseek-v4-flash`；`KEYWORD_HOMONYM_MODE` 目前只实现 `skip`，`playful` 留接口。

## 14. 开放问题（评审时定）

已定（10-24 评审）：复读走**主动消息**；复读**不占用 `REPLY_COOLDOWN`**；黑名单**也挡关键词路径**；兜底句固定为**「什么意思」**；过渡上下文默认 **15** 条；静默词**维持三个不扩**；@ **不关闭 `AWAKE` 窗口**；`RATE=0.2`、`RATE_IN_WINDOW=2.0`（5 人小群校准）；**删除 `WINDOW_REPLIES`**；`DAILY_ACTIVE=120`。

仍开放：

1. `IMAGE_RATE_MULT` 默认 0.5 是否合适（看 vision 成本与实盘观感）。
2. `KEYWORD_CONTEXT_MESSAGES=15` 的 token 成本与真实调用频次。
3. 过渡句提示词的具体写法（P2 再写）。
