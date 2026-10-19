# qq_bot_official

QQ 开放平台（官方 API）专用的 AI 群聊/私聊机器人。基于 NoneBot2 + `nonebot-adapter-qq`，
通过 WebSocket 接收事件，复用一套平台无关的对话核心（记忆、提示词、工具、模型网关）。

## 能力

- 群聊 @ 机器人 / 私聊对话，支持多段回复（仅首条被动回复带引用）
- 长期记忆（PostgreSQL）：个人线程、群摘要、语义记忆、术语库、称呼
- 工具：联网搜索、表情包、改图、记忆读写、历史查询
- 指令：`/help`、`/reset`、`/usage`、`/model`、`/pixiv`、`/whoami`
- 群主开启"接收全量消息"后，可记录普通群消息用于群上下文

## 快速开始

```bash
uv sync
cp .env.example .env     # 填 AppID/AppSecret、PG、各 API Key
./run_bot.sh
```

启动成功的日志：

```
Loaded adapters: QQ
QQ | Bot <AppID> connected
```

## 配置（.env 关键项）

| 键 | 说明 |
|---|---|
| `QQ_BOTS` | 机器人 AppID/AppSecret/intent（JSON）|
| `QQ_IS_SANDBOX` | 是否沙盒 |
| `QQ_GROUP_FULL_MESSAGES` | 群主开启全量消息后设 true |
| `PG_*` | PostgreSQL 连接 |
| `STICKERS_DIR` / `PIXIV_CACHE_DIR` | 表情/图片缓存目录 |
| `DEEPSEEK_API_KEY` / `GLM_API_KEY` / `TAVILY_KEY` | 模型与搜索密钥 |

## 结构

```
qq_bot/
├── platform/           # 端口（Incoming/ReplyTarget）与 QQ 适配器
├── chat/               # 一轮对话核心、提示词、落库、用量
├── services/           # 模型工厂、记忆、表情同步、会话模型
├── tools/              # 工具注册与实现
├── memory/             # PostgreSQL 仓储、摘要、称呼、术语
├── ai/                 # 模型网关
└── plugins/qqbot_official/  # 事件接线与指令
prompts/chat/           # chain.yaml + 各模块
```

## 平台限制

- 成员接口只返回 openid，无昵称；成员清单可能不完整
- 发送端不支持真正的 @ 某人
- 被动回复每条消息有次数上限（当前按 4 段控制）
- 定时提醒/主动消息暂未实现（工具已按能力关闭）
