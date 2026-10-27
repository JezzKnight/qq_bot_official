# qq_bot_official

QQ 开放平台（官方 API）专用的 AI 群聊/私聊机器人。基于 NoneBot2 + `nonebot-adapter-qq`，
通过 WebSocket 接收事件，复用一套平台无关的对话核心（记忆、提示词、工具、模型网关）。

## 能力

- 群聊 @ 机器人 / 私聊对话，支持多段回复（仅首条被动回复带引用）
- 长期记忆（PostgreSQL）：个人线程、群摘要、语义记忆、术语库、称呼
- 工具：联网搜索（fast/standard/deep；证据不足时自动换 query 补查一次）、表情包、记忆读写、历史查询
- 视频理解：用户发送视频/转发视频链接，yt-dlp 获取、ffmpeg scdet 抽关键帧、B 站 CC 字幕辅助，视觉模型总结；默认后台处理，先回“稍等”，不阻塞当前对话；分析成功的笔记写入 PostgreSQL `video_notes`，同一视频下次直接复用
- 指令：`/help`、`/reset`、`/reset group`（管理员）、`/reset all`（管理员）、`/usage`、`/model`、`/whoami`、`/reboot`（管理员）
- 群主开启"接收全量消息"后，可记录普通群消息用于群上下文

## 快速开始

```bash
sudo apt install -y ffmpeg   # 视频抽帧依赖
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
| `QQ_BOTS` | 机器人 AppID/AppSecret/intent（JSON；按钮面板需要 `interaction:true`）|
| `QQ_IS_SANDBOX` | 是否沙盒 |
| `QQ_GROUP_FULL_MESSAGES` | 群主开启全量消息后设 true |
| `PG_*` | PostgreSQL 连接 |
| `STICKERS_DIR` | 表情包目录 |
| `DEEPSEEK_API_KEY` / `GLM_API_KEY` | 模型与搜索密钥 |
| `BILIBILI_COOKIE` | B 站登录 Cookie（AI 字幕需要；至少 `SESSDATA=...`）|
| `VIDEO_*` | 视频上限/帧数/模型等，见 `.env.example` |

## 结构

```
qq_bot/
├── platform/           # 端口（Incoming/ReplyTarget）与 QQ 适配器
├── chat/               # 一轮对话核心、提示词、落库、用量
├── services/           # 模型工厂、记忆、表情同步、会话模型
├── tools/              # 工具注册与实现
├── search/             # 搜索管线（fast/规划/重排/reader/缓存）
├── video/              # 视频来源识别、下载、B 站 CC 字幕、scdet 抽帧、视觉分析
├── memory/             # PostgreSQL 仓储、摘要、称呼、术语
├── ai/                 # 模型网关
└── plugins/qqbot_official/  # 事件接线与指令
prompts/chat/           # chain.yaml + 各模块
```

## 平台限制

- 成员接口只返回 openid，无昵称；成员清单可能不完整
- 发送端不支持真正的 @ 某人
- 被动回复每条消息有次数上限（当前按 4 段控制）
- 定时提醒/主动消息未实现（相关工具与调度代码已移除）
- 视频工具：单次最多 30 分钟 / 512MB、默认 12 帧；无 ASR，B 站 AI 字幕需要 Cookie；抖音未重点验证

## 部署到服务器（固定 IP + Docker PostgreSQL + systemd）

只需要 `git`、`uv`，视频功能额外要 `ffmpeg`：

```bash
sudo apt install -y git ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone <仓库地址> /opt/qq_bot_official && cd /opt/qq_bot_official
uv sync --frozen

cp .env.example .env && chmod 600 .env
# 必改：QQ_BOTS、QQ_IS_SANDBOX=false、HOST=127.0.0.1（不要 0.0.0.0）、
#      PG_*（指向 Docker 发布的 127.0.0.1:5432）、各 API Key、ADMIN_OPENIDS

uv run python scripts/init_db.py      # 建库（角色沿用 PG_USER；--create-user 可代建）
./run_bot.sh                          # 首次启动时自动建表，无需手动执行 SQL
```

- 表结构由 `qq_bot/memory/migrate.py` 在首次连库时按 `schema/v*.sql` 增量创建，重复启动幂等。
- `deploy/qqbot.service` 是 systemd 模板：复制到 `/etc/systemd/system/` 后 `enable --now`。
- QQ 开放平台 → 开发设置 → IP 白名单 填服务器固定公网 IP；群主需开启「接收全量消息」。
- 日常升级：`git pull && uv sync --frozen && sudo systemctl restart qqbot`。

