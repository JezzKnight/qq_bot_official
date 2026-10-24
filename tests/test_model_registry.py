"""model.yaml 的展示名 / api_model 分离。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qq_bot.ai.model_registry import load_registry

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, *, api_model: str = "deepseek-flash") -> Path:
    config = tmp_path / "model.yaml"
    config.write_text(
        f"""
default_model: deepseek-v41-flash
models:
  - name: deepseek-v41-flash
    api_model: {api_model}
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
  - name: deepseek-v4-pro
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
""",
        encoding="utf-8",
    )
    return config


def test_api_name_maps_display_to_provider(tmp_path: Path) -> None:
    registry = load_registry(_write(tmp_path))
    assert registry.names() == ["deepseek-v41-flash", "deepseek-v4-pro"]
    assert registry.api_name("deepseek-v41-flash") == "deepseek-flash"
    assert registry.api_name("deepseek-v4-pro") == "deepseek-v4-pro"


def test_missing_api_model_falls_back_to_name(tmp_path: Path) -> None:
    registry = load_registry(_write(tmp_path, api_model=""))
    # 空 api_model 在解析时回填 name
    assert registry.api_name("deepseek-v41-flash") == "deepseek-v41-flash"


def test_provider_alias_resolves_back_to_spec(tmp_path: Path) -> None:
    registry = load_registry(_write(tmp_path))
    spec = registry.resolve("deepseek-flash")
    assert spec is not None
    assert spec.name == "deepseek-v41-flash"
    assert spec.provider_model == "deepseek-flash"
