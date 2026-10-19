from pydantic import BaseModel, field_validator


class AiChatConfig(BaseModel):
    # 继承 pydantic BaseModel：自动类型校验 + 从环境变量读取配置
    # 模型端点与参数统一由 model.yaml 提供（qq_bot/ai/model_registry.py）；
    # 本类只保留与模型无关的运行时配置，密钥经 .env 注入环境变量。

    # ── 记忆设置（窗口/压缩参数见 memory.yaml）──
    max_context_tokens: int = 8192                 # 上下文总 token 上限

    # ── 动态记忆注入 ──
    memory_injection_enabled: bool = True  # 群聊动态注入总开关
    glossary_enabled: bool = True  # 术语库注入开关

    # ── 管理员（/reboot）──
    admin_openids: list[str] = []

    @field_validator("admin_openids", mode="before")
    @classmethod
    def parse_admin_openids(cls, v: object) -> list[str]:
        """解析 ADMIN_OPENIDS：JSON 数组 / 逗号分隔 / 单值。"""
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        text = str(v).strip()
        if not text:
            return []
        if text.startswith("["):
            import json
            return [str(x).strip() for x in json.loads(text) if str(x).strip()]
        return [x.strip() for x in text.split(",") if x.strip()]

    # ── 频率控制 ──
    cooldown_seconds: int = 5                       # 单用户冷却秒数

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
