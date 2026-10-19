"""客户端工厂 → 模型网关的薄适配器。

模型端点与参数统一来自 ``model.yaml``（见 qq_bot/ai/model_registry.py），
密钥来自环境变量（.env 在插件加载时经 load_dotenv 导出到 os.environ）。
调用方拿到的仍是带 ``.chat()`` 的 OpenAI 兼容客户端，签名与旧版一致。
"""

from qq_bot.ai.capabilities import CAPABILITY_FIELDS, ModelCapabilities
from qq_bot.ai.image_edit_client import ImageEditClient
from qq_bot.ai.model_gateway import ModelGateway, ModelProfile
from qq_bot.ai.model_registry import (
    ImageEditSpec,
    ModelParams,
    ModelSpec,
    VisionSpec,
    get_registry,
)
from qq_bot.ai.openai_client import OpenaiCompatClient

# 全局网关：按 (base_url, api_key, timeout, connect_timeout) 维护连接池
_EMPTY_REGISTRY_MSG = "model.yaml 未配置任何可用模型"
_gateway = ModelGateway()


class _ImageEditState:
    """图像编辑客户端单例状态（避免 global 语句，同 token_usage 的写法）"""

    client: ImageEditClient | None = None
    signature: tuple[str, str, str] | None = None


_image_edit_state = _ImageEditState()


# ── 配置读取（统一走 model.yaml）────────────────────────────────


def default_model_name() -> str:
    """默认对话模型名（model.yaml 的 default_model）。"""
    return get_registry().default_model_name


def get_model_params(model: str) -> ModelParams:
    """取该模型的有效参数（已合并 defaults）。"""
    return get_registry().params_for(model)


def get_vision_spec() -> VisionSpec:
    return get_registry().vision


def get_image_edit_spec() -> ImageEditSpec:
    return get_registry().image_edit


def _to_explicit_capabilities(spec: ModelSpec | None) -> ModelCapabilities | None:
    """把 yaml 能力字段映射为显式能力层；全为空时返回 None（走内置默认表）。"""
    if spec is None or not spec.capabilities:
        return None
    fields = {f: spec.capabilities.get(f) for f in CAPABILITY_FIELDS}
    if all(v is None for v in fields.values()):
        return None
    return ModelCapabilities(**fields)


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


async def get_vision_client() -> OpenaiCompatClient:
    """取本地视觉模型客户端（复用统一网关，独立端点/密钥/超时）。"""
    vision = get_registry().vision
    return _gateway.get_client(
        vision.base_url,
        vision.api_key,
        default_model=vision.model,
        timeout=vision.params.timeout,
        connect_timeout=vision.params.connect_timeout,
    )


async def get_image_edit_client() -> ImageEditClient:
    """取图像编辑客户端（单例；关键配置变化时重建）。

    图像编辑是独立协议 + 独立出口策略（境内直连、trust_env=False），
    因此不复用 chat 连接池（见 note §61.2 / §70）。
    """
    spec = get_registry().image_edit
    signature = (spec.base_url, spec.api_key, spec.model)
    state = _image_edit_state
    if state.client is not None and state.signature == signature:
        return state.client
    if state.client is not None:
        await state.client.close()
    state.client = ImageEditClient(
        base_url=spec.base_url,
        api_key=spec.api_key,
        model=spec.model,
        timeout=spec.params.timeout,
        n=spec.n,
        size=spec.size,
        prompt_extend=spec.prompt_extend,
        enable_thinking=spec.enable_thinking,
        negative_prompt=spec.negative_prompt,
    )
    state.signature = signature
    return state.client


def get_model_profile(model: str) -> ModelProfile:
    """解析模型画像（平台归属 + 能力元数据）。"""
    spec = get_registry().resolve(model)
    return _gateway.get_profile(
        model,
        base_url=spec.base_url if spec else "",
        explicit=_to_explicit_capabilities(spec),
        platform=spec.platform if spec else None,
    )


async def close_all() -> None:
    """关闭全部模型连接池（生命周期 shutdown 调用，幂等）。"""
    await _gateway.close_all()
    if _image_edit_state.client is not None:
        await _image_edit_state.client.close()
        _image_edit_state.client = None
        _image_edit_state.signature = None
