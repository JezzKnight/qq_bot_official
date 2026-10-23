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

    # ── 群聊自动接话（L1：蹭 @ 触发的被动回复窗口，不耗主动消息额度）──
    group_autotrigger: bool = True  # 总开关；关掉后 full_matcher 只落库不接话
    group_autotrigger_daily_cap: int = 6  # 每群每天最多自动接几句
    group_autotrigger_cooldown_sec: int = 90  # 同群两次自动接话的最小间隔
    group_autotrigger_probability: float = 0.6  # 过阈值后再摇一次概率
    group_autotrigger_min_score: int = 4  # 打分阈值（见 qq_bot/chat/activation.py）
    group_autotrigger_window_seconds: int = 300  # 平台被动窗口：5 分钟
    group_autotrigger_names: str = "大肥鱼,肥鱼,鱼鱼"  # 逗号分隔：文本点到名字 +3
    group_autotrigger_hint: str = "（这没人在叫你，你只是顺嘴插一句）"

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
