from pydantic import BaseModel, field_validator


class AiChatConfig(BaseModel):
    # 继承 pydantic BaseModel：自动类型校验 + 从环境变量读取配置
    # 模型端点与参数统一由 model.yaml 提供（qq_bot/ai/model_registry.py）；
    # 本类只保留与模型无关的运行时配置，密钥经 .env 注入环境变量。

    # ── 记忆设置（窗口/压缩参数见 memory.yaml）──
    max_context_tokens: int = 16384  # 上下文总 token 上限

    # ── 视频处理 ──
    # 默认后台：先 ack，视频分析和回答在 asyncio.Task 里完成，不阻塞主对话。
    video_background_enabled: bool = True

    # ── 动态记忆注入 ──
    memory_injection_enabled: bool = True  # 群聊动态注入总开关
    glossary_enabled: bool = True  # 术语库注入开关

    # ── 管理员（/reboot）──
    # 保持 str：NoneBot/pydantic-settings 会把 list[str] 当 JSON 解析，
    # 逗号分隔的 openid 会在 validator 之前就报 SettingsError。
    admin_openids: str = ""

    # ── 频率控制 ──
    cooldown_seconds: int = 5  # 单用户冷却秒数

    # ── 回复节奏（活人感：像 QQ 连发，而不是一次吐一屏）──
    reply_split_max_chars: int = 30  # 单条超过该字数就按标点/空格切条
    reply_typing_delay: bool = True  # 分段之间按字数模拟打字停顿

    # ── 群聊存在感（P1：0-1 抽奖 + 10 分钟窗口 + 静默 + 复读）──
    # 设计见 docs/spec/group-presence.md；决策在 qq_bot/chat/presence.py。
    group_presence_enabled: bool = True  # 总开关；关掉后只落库，不接话
    group_presence_rate: float = 0.2  # 每条可接消息的抽奖概率（0-1）
    group_presence_window_sec: int = 600  # 中奖后在场窗口：10 分钟
    group_presence_rate_in_window: float = 2.0  # 窗口内倍率：p = rate x 本值
    group_presence_reply_cooldown_sec: int = 15  # 窗口内两次接话的最小间隔
    group_presence_wake_cooldown_sec: int = 300  # 窗口结束到可再抽奖的间隔
    group_presence_daily_active: int = 120  # 每群每天主动消息上限（行为熔断）
    group_presence_mute_sec: int = 1800  # 「闭嘴」静默时长：30 分钟
    group_presence_mute_words: str = "闭嘴,别说了,安静"  # 逗号分隔，须 @bot
    group_presence_mute_ack: str = "好"  # 静默回执；不要用反问句
    group_presence_ignore_openids: str = ""  # 逗号分隔：这些人的消息不触发主动
    group_presence_image_rate_mult: float = 0.5  # 纯图片抽奖折扣
    group_presence_repeat_enabled: bool = True  # 复读机开关
    group_presence_repeat_min_users: int = 3  # 至少几个不同用户重复才算
    group_presence_repeat_window_sec: int = 90  # 重复计数窗口
    group_presence_repeat_max_chars: int = 20  # 超过该字数不参与复读
    # P2（关键词）预留：先读入，P1 不启用
    group_presence_keywords: str = ""

    # ── WebSearch配置 ──
    Tavily_key: list[str] = []

    @field_validator("Tavily_key", mode="before")
    @classmethod
    def parse_tavily_key(cls, v: str | list) -> list:
        """解析 Tavily key：支持 JSON 数组、逗号分隔、单值三种格式"""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                import json

                return json.loads(v)
            return [k.strip() for k in v.split(",") if k.strip()]
        return v
