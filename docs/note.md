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
