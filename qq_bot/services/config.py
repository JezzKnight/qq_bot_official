from pydantic import BaseModel, field_validator


class AiChatConfig(BaseModel):
    # 继承自pydantic的基类，能够继承两个能力，一、能够自动进行类型校验 二、自动从环境变量读取配置
    # 模型端点与参数统一由 model.yaml 提供（qq_bot/ai/model_registry.py）；
    # 本类只保留与模型无关的运行时配置，密钥经 .env 注入环境变量。

    # ── Bot 身份 ──
    # bot 自身 QQ 号：扫描群成员时排除自己；未配置时回退到 bot.self_id
    bot_self_id: str = ""

    @field_validator("bot_self_id", mode="before")
    @classmethod
    def parse_bot_self_id(cls, v: object) -> str:
        """QQ 号为纯数字，.env 中不加引号会被 NoneBot 解析成 int，统一转回 str"""
        if v is None:
            return ""
        return str(v)

    # ── 本地 ComfyUI 生图（/comfyui 指令）──
    comfyui_api_base: str = "http://127.0.0.1:8188"  # 本地服务地址
    comfyui_workflow: str = ""    # 工作流 JSON（API 格式）路径，空 = 关闭
    comfyui_upload_node: str = "4"    # LoadImage 节点 id，接用户上传的图
    comfyui_timeout: int = 600    # 排队+生成总超时秒数（SeedVR2 超分较慢）

    # ── 记忆设置（窗口/压缩参数见 memory.yaml）──
    max_history: int = 20                          # 最大历史消息条数
    max_context_tokens: int = 8192                 # 上下文总 token 上限

    # ── 动态记忆注入 ──
    memory_injection_enabled: bool = True      # 群聊动态注入总开关（False → 不注入长期记忆）
    glossary_enabled: bool = True              # 术语库注入开关（匹配命中时是否注入词义）

    # ── 触发设置 ──
    enable_at_trigger: bool = True                   # @ 触发开关
    enable_keyword_trigger: bool = False             # 关键词触发开关
    trigger_keywords: list[str] = ["bot", "机器人"]  # 触发关键词

    # ── 委托式主动互动（interact_with_member）权限 ──
    # 允许发起"帮我去@某位群成员开场"委托的 QQ 号白名单；空列表 = 所有群成员皆可用
    interact_allowed_user_ids: list[int] = []

    @field_validator("interact_allowed_user_ids", mode="before")
    @classmethod
    def parse_interact_allowed(cls, v: object) -> list[int]:
        """解析 env：JSON 数组 / 逗号分隔 / 单数字 / 空值（对齐 parse_tavily_key）"""
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, int):
            return [v]
        text = v.strip() if isinstance(v, str) else ""
        if not text:
            return []
        import json
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        if isinstance(parsed, list):
            return [int(x) for x in parsed]
        if isinstance(parsed, int):
            return [parsed]
        return [int(x) for x in str(parsed).split(",") if x.strip()]

    # ── 频率控制 ──
    cooldown_seconds: int = 5                       # 单用户冷却秒数
    max_daily_calls_per_user: int = 200             # 每人每天最大调用次数

    # ── 回复设置 ──
    reply_max_length: int = 1000                     # 单条消息最大字数

    # ── 启动通知 ──
    startup_notify_group: int = 0              # 启动通知目标群号，0 表示不发送
    startup_notify_cooldown: int = 300         # 启动通知冷却期（秒），防止频繁重启刷屏

    # ── Pixiv访问控制 ──
    proxy: str = ""
    refresh_token: list[str] = []

    @field_validator("refresh_token", mode="before")
    @classmethod
    def parse_refresh_token(cls, v: str | list) -> list:
        """将环境变量中的 JSON 字符串解析为列表"""
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v

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
