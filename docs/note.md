# 项目知识库与问题记录

## 2026-10-21 · 搜索链路重构与双链路基准

**主题**：把“子 Agent 多轮工具调用”改成“检索管线 + 一次回答”。

**核心结论**
- 旧链路的慢不是单点问题，而是结构性的：主模型调用 `search_agent` → 子 Agent 多轮
  `batch_search`/`web_fetch` → 子 Agent 写报告 → 主模型再组织回答；一次问题 3–4 次 LLM，
  且 Tavily 固定 `advanced`。
- 新链路把职责拆开：`SearchPipeline` 负责 query 规划、并行搜索、清洗/去重/重排、按需
  reader；主模型只负责“要不要搜”和“怎么回答”。`quick/standard/deep` 三档控制成本。
- 6 题实测（最终 run4）：总耗时 182.04s → 79.30s（约 2.30×），token 179,435 → 34,365（约省 80.8%）。
  速度/成本收益明确；答案质量在时效题上基本对齐，在需要多源拼图的垂类题上仍有差距。

**架构决策**
- 新增 `qq_bot/search/` 纯服务层，不依赖 NoneBot；`TavilyClient` 可注入配置，便于基准/测试。
- 证据统一成 `[S1]…[Sn]`，最终回答只读证据，不读搜索流水账；这样回答层和检索层可以独立替换。
- 缓存只缓存 Tavily 原始结果，不缓存 reader 正文：正文时效性和页面变化更快，缓存原始结果更安全。
- 保留旧 `SearchAgent` 作为 legacy 基准/兼容，但主模型工具列表已切到 `search`。

**关键文件**
- `qq_bot/search/pipeline.py`：编排与模式参数。
- `qq_bot/search/planner.py`：query 规划与 `infer_time_range`。
- `qq_bot/search/rerank.py`：清洗、去重、相关性/日期/域名多样性、`sort_by_recency`。
- `qq_bot/tools/search_tool.py`：主模型工具 `search`。
- `scripts/search_benchmark.py`：双链路基准。
- `docs/search_benchmark.md`：本次完整报告。

**为什么重要**
- 搜索是 QQ 聊天里最容易“又慢又错”的工具；把检索和回答解耦后，可以单独评估
  “检索召回/时效”和“回答忠实度”，不会一改 prompt 就全盘重测。

## 2026-10-21 · 问题：搜索答案引用过期信息

**问题定义**
- 10 月 21 日问“下一次油价调整”，improved 链路回答“10 月 15 日 24 时”，
  这是已经过去的窗口；实际上下一次是 10 月 29 日。
- 影响范围：所有“最近/下一次/这几天”类时效问题，不只是油价。

**根因剖析**
- Tavily 已经返回了 10 月 21 日的新文章，但重排按“标题/正文词项覆盖”打分，
  旧文章因为标题更匹配而排在前面；最终回答模型先看到旧证据。
- 回答 prompt 没有注入“今天是几号”，模型无法判断 10 月 15 日已经过去。
- 这不是搜索没拿到新结果，而是“证据排序 + 回答时间锚点”两个环节都缺失。

**解决方案**
- `time_range` 非空时，对证据做 `sort_by_recency`：有发布日期的按新到旧，无日期沉底。
- `search_answer.md` 注入 `{{ current_date }}`，并要求“旧日期不要写成今天、旧信息不要当当前状态”。
- 保留相关性打分，但在时效场景让日期排序优先；避免为了时效把完全不相关的新文章顶上来。

**复盘启示**
- RAG/搜索系统里，“检索到”不等于“模型会用”；证据顺序和时间锚点都是 prompt 的一部分。
- 时间过滤（`time_range`）只能限制候选集，不能保证排序；需要显式的 recency 策略。
- 评估时要构造“答案在最新来源里、旧来源看起来很相关”的对抗样本，否则很容易漏掉这类 bug。

## 2026-10-21 · 问题：planner JSON 截断导致空答案

**问题定义**
- 第一次跑 improved 时，`uma_next` 的 query 规划结果被截断成 `['["赛马娘...']`，
  Tavily 收到损坏 query，reader 抓不到正文，最终 answer 为空。

**根因剖析**
- 规划调用把 `max_tokens` 设得偏小，模型输出 JSON 数组时被截断；
- 解析器只接受严格 JSON，截断后直接回退/产生坏 query，没有做容错。

**解决方案**
- 解析器兼容 JSON 数组、`{"queries": [...]}`、逐行列表、双重编码数组和截断数组；
- 规划调用 `max_tokens=600`，并限制 query 数量/长度；
- 解析失败回退原问题，保证“至少还能搜一次”。

**复盘启示**
- 让 LLM 输出结构化数据时，必须把“截断”当成正常失败模式，而不是异常；
- 解析器要保守：宁可回退原问题，也不要把半截 JSON 当 query 发给搜索 API。

## 2026-10-21 · 结论：DeepSeek 自研 Search API 是目前最快的搜索方案

**背景**
- 当前 improved 搜索链路平均 13.2s/题，主要成本是 planner + Tavily + reader。
- 为评估 Tavily 之外的方案，实测了智谱 Web Search、Exa/Brave 公开基准，以及 DeepSeek 自研 Search API。

**DeepSeek Search 实测（6 题，count=5，need_content=false，summary 拼 3800 字以内，回答用 deepseek-v4-flash）**
- 搜索中位数 0.34s，最大 0.39s；6/6 成功，无空结果。
- 端到端（搜索 + 回答）中位数 2.80s，最大 3.20s；总 token 13,307（平均 2,218/题）。
- 对比当前 improved：13.2s / 34,365 token；约快 4.7×，token 省约 61%。
- 质量：S 赛、油价、赛马娘卡池可用；动漫可用；天气因问题未指定地点只答深圳；赛马娘下月大赛诚实说明缺官方赛程。
- Search API：`POST https://api.deepseek.com/v1/tools/search`，字段 `query/count/ori_question/need_content`；返回 `rerank_score/title/url/snippet/summary/content/date/authority`。
- 计费：5 元/1000 次成功请求；Search 100 QPS，Fetch 25 QPS。
- 同一 key 也可用 Responses API 的 `web_search` 工具（仅 deepseek-v4-pro）：单题 5.01s / 3,599 token，正确但比“Search API + flash”慢。

**为什么重要**
- 搜索阶段从秒级降到亚秒级，且 `summary` 本身已压缩，天然适合控制主模型上下文。
- 不需要 planner 和 reader 就能达到 2.8s，说明之前的瓶颈主要是 Tavily + 多轮结构，不是模型回答。
- `date`/`authority`/`rerank_score` 可直接用于时效排序和来源可信度，不需要再猜发布日期。
- 建议新增 provider 抽象：DeepSeek Search 作为 fast 默认，Tavily/智谱作为 fallback；不要把供应商硬编码在 pipeline 里。

**待办**
- 尚未改代码；下一步可落 `DeepSeekSearchProvider` + `fast` 模式（单 query、summary、无 reader、context 3000 字）。
## 2026-10-22 · 落地：DeepSeek Search 作为 fast 搜索

**背景**
- 上一轮实测 DeepSeek Search API：搜索中位 0.34s、端到端中位 2.80s，明显快于
  Tavily + planner + reader 的 13.2s；决定把它接成默认 `fast` 模式。
- fast 只做“单 query + summary”，不调用 planner/reader；standard/deep 继续走 Tavily。

**改动**
- 新增 `qq_bot/search/deepseek.py`：`DeepSeekSearchProvider` 封装
  `POST https://api.deepseek.com/v1/tools/search`，默认 `count=5`、`need_content=false`，
  把 `summary/date/rerank_score/site_name` 规范化为 `SearchItem`。
- `qq_bot/search/pipeline.py` 新增 `fast` 分支：单 query、5 条结果、无 reader、
  context 上限 3000 字；结果仍走去重重排与时效排序。
- `qq_bot/tools/search_tool.py` 的 `search` 默认 `mode="fast"`，保留
  `auto/quick/standard/deep`；provider 复用 `DEEPSEEK_API_KEY`。
- 修复 `build_context()` 在截断时可能超出 `max_chars` 1 个字符的问题，并补回归测试。

**6 题预演（生产代码 + `deepseek-v4-flash` 回答）**
| 问题 | 耗时 | token | 结果 |
| --- | ---: | ---: | --- |
| 最近英雄联盟S赛赛程 | 4.42s | 2,613 | 给出 S16 入围赛/瑞士轮/淘汰赛/决赛时间 |
| 最近新出了什么动漫 | 3.05s | 2,653 | 列出 2026 秋季新番与开播日 |
| 下个月赛马娘大赛什么马强势 | 5.44s | 3,060 | 没查到 2026-11 可靠资料，明确说明 |
| 这几天什么地方天气怎么样 | 3.52s | 2,494 | 只覆盖深圳，说明证据范围 |
| 赛马娘日服新卡池/活动 | 5.22s | 3,075 | 给出万圣节卡池与活动截止日 |
| 下一次国内油价调整 | 1.99s | 1,389 | 正确答 2026-10-29 24:00 |

合计 23.62s / 15,284 token，平均 3.94s、中位 3.97s；搜索阶段中位约 0.41s。
6/6 非空，无 error。

**结论**
- 速度瓶颈确实主要在 Tavily + planner + reader，而不是回答模型；换 provider 后
  搜索阶段进入亚秒级，端到端主要剩一次回答调用。
- fast 适合“新闻/赛程/活动/价格”等单点事实；需要多源拼图、对比、深度核验时
  仍应让模型显式选择 standard/deep。
- 回答质量仍受 query 表达影响：天气问题没写地点时只能按搜索结果覆盖范围回答，
  这是输入信息不足，不是链路故障。

## 2026-10-22 · 功能：搜索结果不足时自动补查一次

**背景**

- `fast` 模式通常 2–4 秒返回，但单条 query 一旦把“赛事/活动/服务器”假设错，
  模型会诚实回答“没查到”，用户看到的是一次失败。
- 实测「下个月赛马娘大赛什么马比较强势」：第一次 fast 把 11 月当成
  冠军会议（CM），回答没查到；实际日服 11 月是 League of Heroes（LoH，
  京都 3000m）。这不是搜索不到，而是 query 的赛事名错了。

**实现**

- 新增 `qq_bot/search/retry.py`：
  - `looks_insufficient()` 检测“没查到/无法确认/资料不足”等明确无结果表述；
  - `build_retry_queries()` 用一次轻量 LLM 调用生成 2–3 条不同角度 query，
    要求展开赛事全名/日文名/服务器/官方日程；解析失败回退通用变体。
- `SearchPipeline.run_retry()`：fast provider 并行检索改写后的 query，
  复用去重、重排、时效排序，context 仍限制 3000 字。
- `qq_bot/tools/search_tool.py` 增加 `retry_search()`，只返回补查证据，不暴露给主模型。
- `qq_bot/chat/turn.py`：只在“本轮调用过 search + 回答明确没查到 + 还没补查”时：
  先发 `这个我得好好查查`，再补查一次；有新证据时把原回答和补查证据追加到消息，
  让主模型重新组织答案；仍无证据则保留原回答。默认最多一次，避免循环。
- 群回复的 `max_parts` 会扣除已发送的 ack，避免补查后超段。

**实际预演（2026-10-22，生产代码 + deepseek-v4-flash）**

- 首轮：搜索 394ms，回答 5.67s，3,104 token，回答明确“没查到 2026-11 CM”。
- 补查：改写 3 条 query，搜索 481ms，回答 4.78s，2,909 token；
  新证据找到 11 月 LoH 京都 3000m，回答从“没查到”变成“LoH 先行强势……”。
- 代价：只有低置信度时才多一次改写 + 搜索 + 回答；正常问题仍走单次 fast。

**踩坑**

- 第一次给改写模型 `max_tokens=400`，推理模型把 400 token 全花在 reasoning 上，
  `content` 为空，只能走通用回退，补查等于没补。
- 将预算提高到 1000 后，模型输出 626 completion（其中 533 reasoning），
  正常生成 JSON。结构化输出 + reasoning 模型时，token 预算必须包含推理开销。

**复盘启示**

- “没查到”不一定是数据不存在，可能是实体/赛事名假设错误；补查的核心是
  **改写 query**，不是原 query 再搜一次。
- 重试必须有硬上限和明确触发条件；否则会把一次 3 秒的回答拖成多次搜索。

## 2026-10-22 · 功能：视频理解工具（ffmpeg + yt-dlp + B 站 CC）

**目标**

- 用户发来 QQ 视频文件、B 站/抖音链接或转发卡片时，让主模型能“看”视频：
  画面用 ffmpeg 场景检测抽关键帧，语音优先用视频自带 CC 字幕，不额外部署 ASR。

**实现要点**

- 来源归一化：直链按扩展名；B 站识别 `BV/av/b23.tv`；抖音识别
  `douyin.com/video` 与 `v.douyin.com`；QQ `video`/`file` 段和 `ark` 转发卡片
  都走同一套 URL 提取。
- 获取：直链用 `httpx` 流式下载并限制 80MB；平台页交给 yt-dlp，
  低清优先（`height<=480`），临时目录在分析结束后删除。
- 抽帧：`ffmpeg -vf scdet=threshold=10,metadata=print:file=-` 解析
  `pts_time`，场景点均匀取样，最多 12 帧；scdet 失败时回退等间隔抽帧。
  每帧单独 `-ss` 抽 JPEG，缩放到 768px 宽，避免把整段视频解码进内存。
- 字幕：`x/web-interface/view` 取 cid，`x/player/v2` 取字幕列表，
  再下载 `subtitle_url` 的 JSON，按 `from/content` 转成 `[mm:ss] 文本`。
- 分析：字幕 + 带时间戳关键帧一起交给视觉模型，输出总结/时间线/
  针对问题的回答/不确定信息；没有字幕时明确提示“不要假装听见”。
- 接入：视频不再塞进 `media_urls`（否则会被当成 `image_url`）；
  改为 `current_videos` + 提示，只有当前消息有视频时才向主模型暴露
  `watch_video` 工具。

**B 站 CC 字幕的实测事实（2026-10）**

- `x/web-interface/view` 匿名可用，返回 bvid/aid/cid/title/duration。
- `x/player/v2` 匿名通常返回 `subtitle.subtitles=[]`，并带
  `need_login_subtitle=true`；登录 Cookie 才可能拿到 AI 字幕。
- UP 主公开的 CC 字幕在部分视频匿名可见，所以代码不预设“一定没有”，
  有就下载，没有就降级为纯画面理解，不启动 ASR。
- `subtitle_url` 可能以 `//aisubtitle...` 开头，需要补 `https:`；
  字幕 JSON 的正文在 `body[].content`，时间在 `body[].from`。

**验证与限制**

- 视频/适配器相关测试共 25 个通过；本地 3 秒 testsrc 端到端通过：
  3 帧、4.76s、视觉模型正常返回。
- 真实视觉模型对 1 张图 500 token 可用；50 token 会被 reasoning 吃光，
  因此视频分析保留至少 1600 token。
- 当前限制：5 分钟/80MB、12 帧、无 ASR；抖音未重点验证；
  B 站 AI 字幕需要 `BILIBILI_COOKIE`。
- 安全：URL 只允许 http(s)，拒绝 localhost/私有 IP；直链下载限制大小，
  yt-dlp 有超时；但 DNS rebinding 仍未做完整防护。

**复盘启示**

- “视频理解”不等于“把视频 URL 塞给视觉模型”；必须先区分直链和网页，
  网页要解析元数据/字幕，视频文件才走 ffmpeg。
- 抽帧的关键不是“每秒一张”，而是先找镜头切换，再在镜头内均匀取样；
  否则长视频会被开头几秒占满。
- 字幕是性价比最高的语音信息源；拿不到字幕时应明确降级，而不是偷偷
  用一个没部署的 ASR 造成“看起来能分析”的假象。



## 2026-10-22 · 修复：视频抽帧黑屏与 B 站字幕错配

> 本节是对上一节“视频理解工具”的实测修正。上一节写的 `x/player/v2`
> 和“最多 12 帧”已被真实视频验证推翻，以本节为准。

**问题现象**

用户发 B 站视频后，模型只看到片头/片尾的黑屏或转场，且始终没有字幕；
一次 1 分钟视频约 9 秒、4.5 分钟视频约 14 秒就返回，看起来像“根本没处理视频”。

**根因 1：scdet 输出被 4KB 截断**

`qq_bot/video/frames.py` 的 `_run()` 曾对所有子进程统一 `max_output=4000`。
但 `ffmpeg -vf scdet=...,metadata=print` 会逐帧打印 `pts_time`，4.5 分钟视频的
输出远超 4KB；截断后只解析到前 2 秒，`select_frame_times()` 又只保留
`[0, duration-0.1]`，最终必然只抽到开头和结尾两帧。

修复：
- `_run(..., max_output=...)` 允许调用方关闭截断；scdet 传 `None`。
- `parse_scdet_times()` 优先解析 `lavfi.scd.time`，否则用
  `lavfi.scd.score >= threshold` 过滤，不再把每一帧都当场景。
- `select_frame_times()` 改为按时间分桶：每桶优先取场景点，没有场景点取桶中心；
  目标帧数 `min(max_frames, max(4, duration//10+1))`。即使 scdet 完全失败，
  也会覆盖整段视频。
- 真实 4.5 分钟视频验证：21 个场景点，最终 8 帧，画面不再是黑屏。

**根因 2：`x/player/v2` 的字幕会错配**

实测同一个 BV 号、带 Cookie 请求 `x/player/v2`，不同参数组合返回了
TED-Ed 环保诗、脑科学、JK 短剧、西游记等完全无关的字幕。`subtitle_url`
存在并不代表字幕属于这个视频，直接下载会把错误文本喂给视觉模型。

修复：
- 改用 `/x/player/wbi/v2`，先请求 `/x/web-interface/nav` 取 `wbi_img`，
  用 B 站 64 项混淆表生成 `mixin_key`，再计算 `wts + w_rid` 签名。
- 不再回退旧的 `x/player/v2`；拿不到就明确降级为纯关键帧，不启动 ASR。
- 真实 BV 验证：WBI 返回 2280 字正确字幕（“观前提醒 / 万圣暗猫……”），
  与画面一致；旧接口返回的错配字幕被彻底绕过。

**根因 3：Cookie 只给了字幕，没有给 yt-dlp**

`BILIBILI_COOKIE` 原先只用于字幕请求；yt-dlp 只认 `VIDEO_YTDLP_COOKIES`
文件，所以“填了 Cookie”对下载仍然无效。修复：把 `SESSDATA=...` 转成临时
Netscape cookie 文件，下载时传 `--cookies`；登录态验证 `isLogin=True`。

**速度结论**

- 修复前：12 帧 + 3000 字字幕，DeepSeek 首次把 3600 token 全花在 reasoning，
  content 为空，重试后总耗时 42.3s。
- 优化后：8 帧 + 6000 字上限 + `reasoning_effort="low"`，真实视频 20.27s；
  6 帧 21.73s（推理量反而波动），4 帧测试遇到 yt-dlp 临时超时，未采用。
- 对比 `glm-5.3-flash`：12 帧/12756 字字幕、15006 prompt token、91.4s，
  不适合作为默认视频模型。
- 20s 是当前 DeepSeek 视觉链路的实际下限；想再快只能换非 reasoning 视觉模型、
  并行分帧或继续减少帧/字幕。

**测试**

- `tests/test_video_pipeline.py` 15 passed；全量 `pytest -q` 130 passed。
- 新增 WBI 标准示例测试（mixin key `ea1db124af3c7062474693fa704f4ff8`）、
  WBI 请求带 Cookie/`w_rid` 测试、无字幕提示测试。
- 视频相关定向 `ruff check` 通过；`qq_bot/ai/openai_client.py` 仍有
  改动前既有 lint 问题，本次只新增 `reasoning_effort` 参数，不扩大范围修。

**复盘启示**

- 子进程日志/元数据输出不能用固定小上限截断；先确认输出协议，再决定是否
  采样或落文件。`scdet` 这种“逐帧 metadata”尤其容易踩坑。
- 平台接口“有 URL”不等于“内容正确”。B 站字幕这类接口必须用官方签名
  （WBI）和真实视频交叉验证，否则会静默喂错数据。
- 视觉模型的 reasoning token 会吃掉输出预算；对视频这种高 prompt 场景，
  应显式限制推理强度或换非 reasoning 模型，不能只看单次调用耗时。


## 2026-10-22 · 修复：看过视频后下一轮“失忆”

**问题现象**

用户在 21:22 发 B 站视频，bot 正常抽帧、拿字幕并回复“碧蓝幻想的万圣节角色
评测 暗猫被夸上天…”，数据库也写入了这条回复。22:10 用户问“暗猫是谁”，
bot 调 `query_chat_history` 失败两次，随后连续回答“不认识/我记性不太好”。

**根因 1：token 估算把 system prompt 放大了一倍，裁掉了整段个人线程**

`qq_bot/memory/token_est.py` 旧实现按“中文 2 token/字”估算。实际 system
prompt 5326 字，估算 7886 token；加上群窗口 493、个人线程 568、群摘要 486，
总估算 8956 token，超过 `max_context_tokens=8192`。

`trim_messages()` 从最旧对话开始裁，先裁群窗口，再裁个人线程。视频那一轮
（id 153/154）正好是个人线程最旧的消息，被裁掉；最后只剩 22:10-22:12 的
“不认识/记性不好”几轮，模型自然继续否认。

修复后按“中文 1 token/字”估算，同一请求估算约 5.5k token，34 条消息全部
保留；DeepSeek 实际 `prompt_tokens=4894`，证明旧估算过于保守。

**根因 2：`query_chat_history` 的 SQL 把 openid 当 bigint**

`qq_bot/memory/pg_repo.py:messages_by_date()` 写的是
`WHERE ($1::bigint IS NULL OR group_id = $1)`，但 v009 迁移后
`messages.group_id` 是 TEXT openid。传 `C69128F...` 会直接报
`invalid input syntax for type bigint`，所以用户追问时两次查询都失败。

修复：改为 `$1::text`；新增 openid 群消息按日期查询的回归测试。

**根因 3：完整视频分析只活在当前轮的 tool 消息里**

`persist_turn()` 只写 user + assistant 两条；`watch_video` 返回的完整分析
（标题、时间线、字幕、画面描述）在本轮结束后丢弃。下一轮即使保留最终回复，
也只剩一句“暗猫被夸…”，不足以回答“暗猫是谁”。

修复：`persist_turn()` 新增可选的 `extra_assistant_text`，`run_turn()` 把
`watch_video` 的结果截到 1600 字，以 `[视频分析记录，仅供后续对话参考]`
写入个人线程；这条消息不发送给用户，只用于后续上下文。日志增加
`TURN video note chars=...`，方便确认是否写入。

**验证**

- 全量 `pytest -q`：133 passed。
- 用真实 system prompt + 最近 12 条线程重放：修复前 34 条被裁到 5 条；
  修复后 34 条全保留，`prompt_tokens=4894`。
- 仅修复估算后，模型能看到旧回复但仍说“我瞎编的”；插入一条视频分析记录后，
  同一 prompt 正确回答“暗猫是碧蓝幻想那个万圣节暗属性角色，视频里说很强、
  打 10 分推荐抽”。说明“保留最终回复”和“保留分析证据”缺一不可。
- `messages_by_date` openid 测试通过；`persist_turn` 额外笔记测试通过。

**复盘启示**

- 上下文预算的 token 估算必须贴近真实 tokenizer。过度保守的估算不是“安全”，
  而是会把最需要的历史裁掉；估算器本身要有回归测试。
- 用户说“失忆”时，先查“消息是否落库”和“落库后是否被裁掉”，不要先怪模型。
- 工具产生的中间分析如果对后续对话有价值，就要显式持久化；只存最终回复
  等于把证据丢了。持久化时用“仅供后续参考”的标记，避免模型把它当成用户可见发言。
- openid 改造后所有 SQL 参数类型都要重新检查，`$1::bigint` 这种旧写法很容易漏。

## 2026-10-22 · 修复：搜索补查提示被最终回复抢先

**问题**：搜索补查时 `这个我得好好查查` 没有先到，而是和补查后的最终回复一起出现。

**根因**：补查提示走 `QQReply.text()`，它是主动消息；最终回复首段走 `mention()`，
是被动回复。QQ 对主动消息有排队/限流，补查又只需几秒，最终回复可能先被客户端收到。
视频提示没有这个问题，因为视频分析要 20 秒左右，主动消息有足够时间先送达。

**修复**：`ReplyTarget` 增加 `passive()`，补查提示改用 `incoming.reply.passive()`，
与最终回复走同一被动通道；`reply_parts_used` 仍计入一次回复额度。补查测试断言
`retry_search` 被调用前，ack 已经出现在发送列表里。

**教训**：需要“先发一条短提示，再继续耗时处理”时，不能只看 `await send()` 是否返回；
要确认提示和最终答复走的是同一条平台通道，否则服务端排序可能反过来。

## 2026-10-22 ~ 10-23 · 事实问答约束的取舍

**背景**

- `persona.md` 写“配 `ee` 表情包”，但表情包正式名称是“等饭”，`ee` 只是旧标签；已改成“发表情包‘等饭’”，`multimodal.md` 也要求正文用目录里的正式名称。
- 赛马娘这类“下个月大赛 + 强势马 + 种马技能”的问题，最初想靠专用 prompt 让它一次给出具体答案。

**尝试过、但已回退**

- 新增过 `prompts/chat/research.md`，chain 升到 v5，写入了“日服 11 月可能是 LoH”“第一次 query 只写游戏名/服务器/年月/大赛/攻略”“不要预写冠军会”等规则。
- 这些规则对单个问题有效，但本质是把特定游戏、特定月份的答案写进系统提示：场景一多就不适用，还持续占用 prompt token，属于过拟合。
- 已删除 `research.md`，chain 回到 v4；`search_answer.md` 也改回通用规则，不再包含游戏赛事特判或来源要求。

**最终方案**

- `search` 工具仍固定 fast，只暴露 `query`；但检索上限从 8 条/4000 字提高到 **10 条/5000 字**，用更宽的召回覆盖场景。
- `search` 的 description 只保留通用要求：“query 尽量具体，带时间、对象、地区；结果不足会自动换关键词补查一次”，不再写任何游戏/赛事特判。
- 回复不再要求提来源、URL 或相关链接；只回答内容本身。
- `style.md` 保留“不要 Markdown 加粗/斜体”，这是通用输出约束。

**验证**

- 全量 `pytest -q`：137 passed；相关 ruff 检查通过。
- 之前用 `research.md` 跑出的 139 passed 和赛马娘专用答案属于已回退版本，不再作为当前实现依据。

**复盘启示**

- 不要用系统提示去补单个问题的答案；这类“补丁”会把临时结论固化成长期偏见。
- 遇到“查得不够准”，先提高召回上限、改 query 策略或换检索源；只有跨场景稳定复现的缺陷才值得进 prompt。
- 通用约束应写在工具描述/输出规范里，并且保持短；场景特判越多，维护成本和 token 成本越高。


## 2026-10-22 · 配置：视频上限提升到 10 分钟

**背景**

- 原先视频工具按 5 分钟 / 80MB 配置；用户需要至少解析 10 分钟的视频。
- `.env` 里虽然已手动改成 600/160，但 `config.py` 的默认值和 `.env.example` 仍是 300/80，
  注释也还写着 5 分钟 / 80MB，换环境或删掉 `.env` 后会退回旧限制。

**改动**

- `VideoConfig` 默认 `max_duration=600`、`max_bytes=160MB`。
- 下载超时默认提高到 180s，yt-dlp 超时提高到 240s，避免 10 分钟视频在慢网络下被提前杀掉。
- `.env.example` 与 `.env` 同步为 600/160，并补充两个超时键。
- 明确注释：`VIDEO_MAX_FRAMES` 是上限，不是固定每 10 秒抽一帧；
  选帧优先 scdet 场景帧，场景不足时才均匀补帧。
- README 的平台限制同步改为 10 分钟 / 160MB。

**边界**

- 10 分钟是建议上限，不是硬拒绝；超过仍会处理，但结果里会带“超过建议上限”的提示。
- 直链下载会硬性拒绝超过 160MB；B 站/抖音走 yt-dlp，目前没有单独的文件大小硬限制。
- 帧数上限仍是 8；10 分钟视频会按时间分桶从场景点里取 8 帧。需要更细的覆盖时再调 `VIDEO_MAX_FRAMES`。
