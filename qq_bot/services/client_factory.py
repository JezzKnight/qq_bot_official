"""客户端工厂 → 模型网关的薄适配器。

模型端点与参数统一来自 ``model.yaml``（见 qq_bot/ai/model_registry.py），
密钥来自环境变量（.env 在插件加载时经 load_dotenv 导出到 os.environ）。
调用方拿到的仍是带 ``.chat()`` 的 OpenAI 兼容客户端，签名与旧版一致。
"""

from qq_bot.ai.model_gateway import ModelGateway
from qq_bot.ai.model_registry import ModelParams, get_registry
from qq_bot.ai.openai_client import OpenaiCompatClient

# 全局网关：按 (base_url, api_key, timeout, connect_timeout) 维护连接池
_EMPTY_REGISTRY_MSG = "model.yaml 未配置任何可用模型"
_gateway = ModelGateway()


# ── 配置读取（统一走 model.yaml）────────────────────────────────


def default_model_name() -> str:
    """默认对话模型名（model.yaml 的 default_model）。"""
    return get_registry().default_model_name


def get_model_params(model: str) -> ModelParams:
    """取该模型的有效参数（已合并 defaults）。"""
    return get_registry().params_for(model)


# ── 客户端获取 ───────────────────────────────────────────────


async def get_client_for_model(model: str) -> OpenaiCompatClient:
    """按模型名取（复用）OpenAI 兼容客户端；未命中回退默认模型。

    不同模型可能指向不同端点/密钥/超时，因此必须按组合取对应客户端，
    不能用单一全局单例。
    """
    spec = get_registry().resolve(model)
    if spec is None:
        raise RuntimeError(_EMPTY_REGISTRY_MSG)
    params = spec.params
    return _gateway.get_client(
        spec.base_url,
        spec.api_key,
        default_model=model,
        timeout=params.timeout,
        connect_timeout=params.connect_timeout,
    )


async def close_all() -> None:
    """关闭全部模型连接池（生命周期 shutdown 调用，幂等）。"""
    await _gateway.close_all()
