# 活人感调研：别人家的 QQ 机器人是怎么做的

> 调研范围：MaiBot（麦麦）、AstrBot 生态（group_chat_plus / private_companion）、ChatLuna `chatluna-character`、nonebot-plugin-personification、若干分段/延迟技巧。
> 结论先行：**这些项目没有一个是靠"把 prompt 写得更像人"做出活人感的**。它们把"不完美"交给代码去随机化，prompt 只负责人格和判断。我们前 7 版都在改 prompt，方向本身就是错的。

---

## 一、我们卡在哪

先把现状钉死，否则讨论会飘。以下全部是代码事实：

| 环节 | 我们现在的实现 | 位置 |
| --- | --- | --- |
| 什么时候说话 | 被动触发：@ 或关键词才回，没有概率、没有沉默、没有时机判断 | 插件 matcher 层，`qq_bot/chat/` 下**不存在** `should_reply` / `random()` |
| 一次说几条 | 按**空行**拆，最多 4 段，超了就把尾部合并 | `qq_bot/platform/reply.py:8 split_for_reply(text, *, max_parts=4)` |
| 段与段之间 | **零延迟**，连续 `await` 发出去，QQ 侧几乎同时到达 | `qq_bot/chat/turn.py:415-429` |
| 打字习惯 | 只有一条：正文里的中文逗号 `，` → 空格 | `qq_bot/platform/qqbot/reply.py:20 normalize_typing` |
| 状态/心情 | 无。没有人设状态、没有作息、没有"正在干什么" | — |
| 主动小动作 | 无。不支持拍一拍、不支持贴表情回应、不支持撤回 | `grep poke` 无结果；入站引用只转成一行文本前缀（`qq_bot/platform/qqbot/incoming.py:61-89`） |
| 表情包 | 有 8 个，带 `name` 和"使用时机"描述，`send_sticker` 工具发 | `stickers.yaml` + `qq_bot/tools/send_sticker.py` |
| 记忆 | 有：短期窗口 + 长期 md + 群文化/称呼 + `save_glossary` | `qq_bot/memory/` |

**关键事实**：`grep asyncio.sleep qq_bot/` 全项目只有两处 —— `qq_bot/ai/openai_client.py:128`（重试退避）和 `qq_bot/plugins/qqbot_official/commands.py:174`（0.5s）。**发送路径上一处都没有。**

所以我们的"活人感"100% 压在 `prompts/chat/*.md` 的措辞上。而 `uncertainty.md` 已经到 v7、45 行硬线，`style.md` 已经写了"1–10 字、一行、群友不是客服、不要分点/Markdown"。

---

## 二、五个标杆，各自的解法

### 1. MaiBot（麦麦）——「一个想当群友的 AI」

它根本不是问答机器人，定位是「潜伏在群里的真人」。

- **HeartFlow 心流**：每个群一个 `SubHeartflow`，持续认知循环，不停"看"群里在聊什么，而不是等 @。
- **三种模式**：普通模式给每条消息打**激活度分数**，过阈值才回，其余时候沉默 —— 沉默是默认状态；专注模式**每 3 秒**观察一轮，自主决定回不回、发语音、禁言、画图；自动模式按群活跃度切换。
- **故意不完美**：打错字、连发碎片消息、大小写不一致。这不是彩蛋，是明确的产品设计。
- **黑话挖掘系统**：自动检测群内高频特定用语，学会后在合适场景用出来 —— 这就是"群友感"的来源，不是 prompt 里写"你要懂梗"。
- **想什么 / 怎么说解耦**：改人格不动推理链。
- **记忆**：短期窗口 + 压缩约 5 倍上下文；长期 LPMM 建在 HippoRAG 2 上（嵌入 → Faiss → 实体抽取 → 知识图谱 → ReAct 检索）；**每用户/每群独立关系记录 + 亲密度分数 + 对群文化的印象**。
- 架构：MaiCore 独立 FastAPI 服务 + 适配器（NapCat）。

> 来源：[baka.studio 拆解文](https://baka.studio/p/maibot%E4%B8%80%E4%B8%AA%E6%83%B3%E5%BD%93%E7%BE%A4%E5%8F%8B%E7%9A%84-ai)、[Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)

### 2. AstrBot Group Chat Plus —— 「把即时回复变成可延迟的参与」

它最值得抄的不是 prompt，是**判断要不要回**的工程：

- **两层过滤**：先概率筛选，再 AI 智能判断。
- **动态概率系统**：回复后概率提升，模拟真人"话头上来了会接着说"的连续性。
- **注意力机制**：多用户同时追踪、情绪检测、注意力指数衰减 —— 久没人理你，你自然就不接了。
- **智能缓存**：没回的消息保留上下文，自动合并补发；冷群自动转正（等话题热起来再说）。
- 冷却期、回复密度、每日上限。

它的自我定位原话：**"群聊机器人最难处理的部分不是生成一句回复，而是判断是否应该回复。过度主动会打断聊天，过度保守又会像离线。"** 以及 **"把『即时回复』变成『可延迟的参与』。"**

> 来源：[astrbot_plugin_group_chat_plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus)、[发言时机设计文](https://sphererapids.com/zh/blog/2026-05-15-group-chat-plus-reply-timing/)

### 3. AstrBot 私聊陪伴插件 —— 「观察 → 理解 → 唤醒 → 续接 → 插话」五层

机制值得抄的地方在于它把"读空气"拆成了可实现的流水线：

- **观察层**：记录文本 + **图片/语音/视频/分享卡片占位**（不是只读文字）。
- **理解层**：维护群气氛、成员观察、黑话、话题线、群聊片段、互动关系。
- **唤醒层**：识别 @、Bot 名、强唤醒词、主要用户专属词、**疑问句信号**、冷群信号。
- **续接层**：判断"对方没 @ 但在跟我说话"，并限制连续轮数（防止自嗨）。
- **插话层**：按**概率 + 冷却 + 每日上限 + 场景 + 关系边界**决定；主动消息必须可解释（候选 → 额度 → 时机 → 权限 → 发送前复核）。
- 状态组件：心情/日程/日记/天气/梦境/位置连续上下文。

> 来源：[astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)

### 4. ChatLuna `chatluna-character` —— 生产级的「伪装群友」预设

这是所有素材里**信息密度最高**的一份，因为它把想法写成了可执行的输出协议。真实预设 `default.yml`（359 行）规定模型每轮必须吐四段结构化内容：

```
<status>心情/状态/记忆/动作</status>
<think>对自己的想法</think>
<action>本次操作</action>
<output><message>…</message><message>…</message></output>
```

细节（每一条都是我们缺的）：

- `<status>` 固定四字段：**心情**（平静/愉悦/烦恼，影响情感倾向）、**状态**（正在闲聊）、**记忆**（最近几小时摘要，≤300 字滚动裁剪）、**动作**（"拿起手机聊天"）。
- **一条话拆多条发**："如果一句话比较长，就将整句话分为多个消息发送"，示例：`<message>诶……这个嘛</message><message>这我就不知道了（）</message>`。
- **回复不超过 30 字**。
- **"消息末尾通常不加句号（无论是。还是.都不加）"**。
- 读空气三连："并非每一条消息都需要回复"、"引用你的消息的人只是在跟别人说话"、**"当一条消息已经过去十分钟以上时，最好不回复，因为已经错过最佳时机"**、"当你延迟回复后看到别人当时的催促，请忽视"。
- `<action>` 里可执行：**戳一戳**、**贴表情回应**（emoji_id + message_id，带 9 个表情的 ID/外观/描述，如"辣眼睛 ID:265 地铁老人手机.jpg"）、**撤回**、以及 **`<next_reply>` 设定下次主动触发条件**（等某人说话 / 等一段时间没人说话）。
- **名词解释表**内嵌在 prompt 里（`wfl：我服了`、`入机：人机，嘲讽别人说话像不聪明的 Bot`）。
- 群友档案（昵称/别名/生日/特点），且注明"90% 的情况下都不应该主动提到"。
- 表情包给的是 **ID + 外观 + 描述**（"辣眼睛 ID:265 地铁老人手机.jpg 描述：比紧张更常用于评价危害性较弱的逆天发言"），不是给个名字让模型猜。

> 来源：[chatluna-character default.yml](https://github.com/ChatLunaLab/chatluna-character/blob/main/resources/presets/default.yml)、[角色插件文档](https://chatluna.chat/ecosystem/other/character.html)

### 5. nonebot-plugin-personification —— 「代码管工程，模型管语气」

它的分工原则一句话：**"把『语气与聊天内容』完全交给大模型自由发挥，而代码负责控制上下文拼装、防重复发送、记忆存储以及并发安全锁。"**

- 流水线：`事件接收与回复缓冲区 → 上下文/人设/记忆/话题状态 → 语义帧决策 → Agent 工具循环 → 回复审核与并发排他锁 → 拟人化打字延迟与发送`。
- **拟人化打字延迟**：按回复字数 + 推理耗时模拟，不是固定 sleep。
- 分段发送、拍一拍、引用回复、**偶尔犯无伤大雅的错别字**。
- **独立作息**：深夜困倦、劝人早睡、拒绝连轴转。
- **动态好感度**：好感升高 → 语气变亲近、称呼变化；被骚扰 → 防卫与边界。
- **多源校验查梗**：只有多个独立来源都认可某个黑话解释才采纳，"防止被网上的虚构段子误导"。

> 来源：[personification 博客](https://blog.shiro.team/posts/personification/)

### 6. 分段与延迟的两个小技巧（可直接用）

- **Amadeus-QQ-MCP**：按句号/逗号/破折号做**自然语义分段**，段数由模型用 `num_chunks` 控制，配 3 秒级速率限制。
- **CowAgent issue #1078**：用 stream，**遇到标点就把已生成部分吐出去**，实现"边想边说"——真人打字就是这样的，不是憋出一整段再发。

> 来源：[Amadeus-QQ-MCP](https://lobehub.com/zh-TW/mcp/julesliu390-amadeus-qq-mcp)、[CowAgent #1078](https://github.com/zhayujie/CowAgent/issues/1078)

---

## 三、横向对照：活人感其实拆成 9 个维度

| 维度 | 标杆做法 | 我们 |
| --- | --- | --- |
| ① 何时说 | 激活度打分 + 阈值 + 概率 + 注意力衰减 + 冷却/上限 | 🟡 L1 已实现：被动窗口内接话（每日每群 ≤6、90s 冷却、打分≥4、0.6 概率）；全天候激活仍未做 |
| ② 何时不能说 | 超过 10 分钟就不回；催促请忽视 | 无 |
| ③ 说几条 | 一条长句拆 2–3 条 `<message>`；或按标点流式吐 | ✅ 代码三级断点分条（档 A，`split_for_reply`），不再依赖模型写空行 |
| ④ 每条多长 | 30 字硬上限 / 1–10 字 | ✅ 30 字硬切（`MAX_CHUNK_CHARS`）+ prompt 建议 |
| ⑤ 不完美 | 错字、碎片、大小写、句末不加句号 | 🟡 逗号→空格 + 段尾句号剥离（档 A）；错字/碎片/大小写仍无 |
| ⑥ 状态 | `<status>` 心情/动作/记忆摘要，跨轮保持 | 无 |
| ⑦ 记忆 | 长期 + 群文化 + 黑话库 + 好感度 + 关系记录 | 有长期 md + 群文化 + `save_glossary`，无好感度、无自动黑话挖掘 |
| ⑧ 感知 | 图片/语音/视频/分享卡片都进上下文 | 有图片 + 视频分析，表情包/分享卡片只当文本 |
| ⑨ 主动 | 戳一戳、贴表情、撤回、`<next_reply>` 定时回来 | 只能整段说话，无小动作 |

**结论（2026-10-24 档 A 之后更新）**：③④ 已变成代码硬约束，⑤ 做了一半，⑦ 是一半；① ② ⑥ ⑧ ⑨ **仍是一行代码都没有**。
档 A 补齐的是"怎么说"（分条、停顿、不加句号），"要不要说"（① ②）和"以什么状态说"（⑥）一步没动 —— 这两层才是活人感的主干。

---

## 四、为什么"再改 prompt"救不了 AI 感

这是本次调研最需要你接受的一条判断，不是审美问题，是机制问题：

1. **模型不能控制自己不说什么。** 说几条、隔多久、要不要回 —— 这些是发送层和决策层的事。你在 prompt 里写"要像 QQ 连发"，它只能靠输出 `\n\n` 来暗示，而 `split_for_reply` 正好按空行拆。于是它每次都要**主动决定"这次拆不拆"**，这个决策本身就会让它变得保守（怕拆错）。
2. **45 条硬线 = 45 个同时要满足的约束，输出必然向"平均、无风险"收敛。** 这就是 AI 感的技术定义：**过度合规**。你回头看 v7 的三次实录，最"像人"的那几句（"是我脸大""脸还在 数据没了"）都不是规则要求出来的，是它偶尔漏出的一点自由度。而我们正在系统性地消灭这点自由度。
3. **真人感来自"不必每句都正确"，而 prompt 只能要求"每句都正确"。** 错字、改口、只回一个字、干脆不回 —— 这些必须由代码随机注入，写进 prompt 就变成"第三个要表演的任务"，反而更假。
4. 佐证：MaiBot 的作者自己承认仍有"比较尬的强行幽默桥段"（[知乎](https://zhuanlan.zhihu.com/p/2038138276364677611)）。强行的幽默就是 prompt 里写"你要有乐子"的必然产物 —— **乐子不能被要求，只能被允许。**
5. 同一篇知乎给的判断很关键：**"拟人化的表达除了 role play system prompt 以外，还需要结合人在消息方面的习惯"** —— 句末不加句号、说话简短、有口癖。这三条里，前两条是**发送层的后处理**，只有第三条是 prompt 的事。

---

## 五、落地路线（按投入分三档）

> 以下只是方案，未动任何代码。

### 档 A：半天，改发送层（性价比最高）—— ✅ 2026-10-24 已实施 1–3 条

1. **真分条** ✅：`split_for_reply` 加了三级断点（句末 → 分句 → 空格），超 30 字自动切，且防劈数字/防断虚词。触点：`qq_bot/platform/reply.py:8`。
2. **段间延迟** ✅：`min(1.8s, 0.35 + 0.07 × 字数)`，首条立即，之后逐段 sleep。实测 30 字左右 ≈ 1.8s，比原方案慢一点，观感更接近真人打字。触点：`qq_bot/chat/turn.py:415-429`。
3. **句末句号剥离** ✅：段尾 `。`/全角句点直接剥，ASCII `.` 避开 `...`；`！？…` 保留。触点：`qq_bot/platform/qqbot/reply.py:20`。
4. **概率沉默** ❌ 前提不成立：`is_group_trigger`（`qq_bot/platform/qqbot/incoming.py:46`）只认 @，群聊没有关键词触发路径，`_FULL_GROUP` 那条只落库不触发 AI。要真做概率沉默，得先有"没 @ 也可能被激活"的判定层，挪到档 C（激活度打分）。

预期：观感变化最明显的一档。分条 + 延迟这两条，几乎所有"机器人味"的反馈都会掉一半。

> **实施补充（2026-10-24）**：先补了 A0 **会话串行锁**（新增 `qq_bot/chat/session_lock.py`，把 `run_turn` 包进 `async with session_lock(...)`）—— 有停顿以后，并发 turn 会交叉插播，锁是延迟的前置条件。顺带修了私聊误调 `reply.mention()` 的 bug，并把分条/延迟做成配置开关（`reply_split_max_chars`、`reply_typing_delay`）。过程记录见 `docs/note.md` 同日条目。

### 档 B：1–2 天，改输出协议（对齐 chatluna-character）

5. **结构化输出**：把 `status / think / action / output` 引进来，心情和动作跨轮保存，prompt 里那些"你别是怎样的"改写"你现在是什么状态"。这能一次性砍掉 `uncertainty.md` 里一半的硬线。
6. **表情包升级为"贴表情回应"**：现在只能整张图发出去；引用式回应（对某条消息贴个表情）更像人。触点：`qq_bot/tools/send_sticker.py`。
7. **受控错字**：闲聊轮 1/30 概率注入一个同音错字。**只在非事实轮触发**，这是活人感与可信度的分界线。
8. **`<next_reply>` 主动回来**：项目已有 `scheduled_tasks`，复用它让模型自己定"等他说话/过 3 分钟没人说话我就回来说一句"。

### 档 C：一周以上，改认知层

9. **黑话自动挖掘**：现在 `save_glossary` 要模型主动存。改成代码侧统计高频陌生词 → 自动检索 → 多源校验（学 personification 的"多源才采纳"）→ 入库。
10. **好感度 / 关系状态**：按互动质量给每个群友打分，影响称呼和语气亲近度。
11. **作息**：深夜变困、少接话、劝人早睡。
12. **未回复缓冲**：合并补发、冷群延后 —— 对齐 group_chat_plus。

**建议顺序：A → B 的 5 → C 的 9**。因为 A 是纯观感收益、零模型风险；B5 能让我们停止在 prompt 里堆规则；C9 才是"群友感"的根。

---

## 六、参考链接

- [MaiBot 拆解：一个想当群友的 AI](https://baka.studio/p/maibot%E4%B8%80%E4%B8%AA%E6%83%B3%E5%BD%93%E7%BE%A4%E5%8F%8B%E7%9A%84-ai) / [Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot) / [README](https://raw.githubusercontent.com/Mai-with-u/MaiBot/main/README.md)
- [AstrBot group_chat_plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus) / [发言时机设计](https://sphererapids.com/zh/blog/2026-05-15-group-chat-plus-reply-timing/) / [爱发电简介](https://afdian.com/a/chat_plus)
- [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)
- [chatluna-character default.yml](https://github.com/ChatLunaLab/chatluna-character/blob/main/resources/presets/default.yml) / [角色插件文档](https://chatluna.chat/ecosystem/other/character.html)
- [nonebot-plugin-personification](https://blog.shiro.team/posts/personification/)
- [知乎：AstrBot + MaiBot 联合部署，兼具功能性与活人感](https://zhuanlan.zhihu.com/p/2038138276364677611)
- [Amadeus-QQ-MCP](https://lobehub.com/zh-TW/mcp/julesliu390-amadeus-qq-mcp) / [CowAgent #1078 流式分段](https://github.com/zhayujie/CowAgent/issues/1078)
- [ElevenLabs 对话式 AI 设计（延迟是最重要的拟人因素）](https://elevenlabs.io/zh/blog/conversational-ai-design)
