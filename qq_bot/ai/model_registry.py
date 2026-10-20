"""模型注册表：从 model.yaml 读取模型端点与参数，密钥仍由 .env 提供。

设计要点（见 docs/note.md §81）：
- YAML 只放"参数"、不放密钥：条目用 ``api_key_env`` 指向环境变量名，值留在 .env。
  这样 model.yaml 可以安全入库，而 .env 继续被 gitignore。
- 纯服务层：不依赖 NoneBot / pydantic，可脱离框架单独导入测试。
- 参数合并：``models[].params`` 覆盖 ``defaults``，缺省项自动继承。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 相对路径按仓库根解析（本文件位于 qq_bot/ai/）
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = "model.yaml"
ENV_CONFIG_PATH = "AI_MODEL_CONFIG"


class ModelConfigError(Exception):
    """model.yaml 缺失或格式非法。"""


def _env(name: str) -> str:
    """按环境变量名取密钥；未配置返回空串（调用方据此降级）。"""
    return (os.getenv(name, "") if name else "").strip()


@dataclass(frozen=True)
class ModelParams:
    """单次对话调用使用的参数。"""

    temperature: float = 1.0
    max_tokens: int = 16384
    timeout: float = 180.0
    connect_timeout: float = 10.0


@dataclass(frozen=True)
class ModelSpec:
    """一个对话模型的完整定义（密钥通过环境变量名间接引用）。"""

    name: str
    base_url: str
    api_key_env: str = ""
    params: ModelParams = field(default_factory=ModelParams)

    @property
    def api_key(self) -> str:
        return _env(self.api_key_env)


class ModelRegistry:
    """model.yaml 的内存视图：按名查模型、取参数。"""

    def __init__(
        self,
        *,
        models: list[ModelSpec],
        default_model: str,
        path: Path,
    ) -> None:
        self._models = {m.name: m for m in models}
        self._order = [m.name for m in models]
        self.default_model_name = default_model or (
            self._order[0] if self._order else ""
        )
        self.path = path

    def names(self) -> list[str]:
        """按 yaml 中的声明顺序返回模型名列表。"""
        return list(self._order)

    def get(self, name: str) -> ModelSpec | None:
        return self._models.get(name)

    def resolve(self, name: str) -> ModelSpec | None:
        """按名取模型；未命中回退默认模型（默认也不存在时返回 None）。"""
        return self._models.get(name) or self._models.get(self.default_model_name)

    def params_for(self, name: str) -> ModelParams:
        spec = self.resolve(name)
        return spec.params if spec else ModelParams()


def _as_params(raw: dict[str, Any] | None, base: ModelParams) -> ModelParams:
    """合并 defaults 与条目级参数。"""
    raw = raw or {}
    return ModelParams(
        temperature=float(raw.get("temperature", base.temperature)),
        max_tokens=int(raw.get("max_tokens", base.max_tokens)),
        timeout=float(raw.get("timeout", base.timeout)),
        connect_timeout=float(raw.get("connect_timeout", base.connect_timeout)),
    )


def _resolve_path(path: str | Path | None) -> Path:
    """解析配置文件路径：显式参数 > 环境变量 AI_MODEL_CONFIG > 默认 model.yaml。"""
    raw = str(path or os.getenv(ENV_CONFIG_PATH, DEFAULT_CONFIG_FILE)).strip()
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else _REPO_ROOT / candidate


def load_registry(path: str | Path | None = None) -> ModelRegistry:
    """读取并校验 model.yaml，返回注册表（失败抛 ModelConfigError）。"""
    cfg_path = _resolve_path(path)
    if not cfg_path.is_file():
        msg = f"模型配置文件不存在: {cfg_path}"
        raise ModelConfigError(msg)
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"model.yaml 解析失败: {exc}"
        raise ModelConfigError(msg) from exc
    if not isinstance(data, dict):
        msg = "model.yaml 顶层必须是映射（mapping）"
        raise ModelConfigError(msg)

    base = _as_params(data.get("defaults"), ModelParams())
    raw_models = data.get("models") or []
    if not isinstance(raw_models, list) or not raw_models:
        msg = "model.yaml 的 models 不能为空"
        raise ModelConfigError(msg)

    specs: list[ModelSpec] = []
    seen: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict) or not item.get("name"):
            msg = "models 条目缺少 name"
            raise ModelConfigError(msg)
        name = str(item["name"]).strip()
        if name in seen:
            msg = f"模型名重复: {name}"
            raise ModelConfigError(msg)
        seen.add(name)
        if not item.get("base_url"):
            msg = f"模型 {name} 缺少 base_url"
            raise ModelConfigError(msg)
        specs.append(
            ModelSpec(
                name=name,
                base_url=str(item["base_url"]).strip(),
                api_key_env=str(item.get("api_key_env") or "").strip(),
                params=_as_params(item.get("params"), base),
            )
        )

    return ModelRegistry(
        models=specs,
        default_model=str(data.get("default_model") or "").strip(),
        path=cfg_path,
    )


class _State:
    """模块级单例（属性赋值，避免 global 语句）。"""

    registry: ModelRegistry | None = None


_state = _State()


def get_registry() -> ModelRegistry:
    """取全局注册表（首次访问时加载）。"""
    if _state.registry is None:
        _state.registry = load_registry()
    return _state.registry


def reload_registry(path: str | Path | None = None) -> ModelRegistry:
    """重新加载（配置热更新与测试用）。"""
    _state.registry = load_registry(path)
    return _state.registry
