"""提示词链路：顺序、版本、平台/多模态条件与配置校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from prompts.loader import PromptLoader
from prompts.renderer import PromptRenderer
from prompts.service import PromptChainError, PromptService, prompt_service

CHAT_DIR = Path("prompts/chat")
BASE_ORDER = ["persona", "constraints", "guidelines", "capabilities"]


def _marker(name: str) -> str:
    content = (CHAT_DIR / f"{name}.md").read_text(encoding="utf-8")
    return content.strip().splitlines()[0][:24]


def _positions(text: str, names: list[str]) -> list[int]:
    return [text.index(_marker(name)) for name in names]


def test_chain_version() -> None:
    assert prompt_service.chain_version == 3
    assert all(ref.version.startswith("v") for ref in prompt_service.modules)


def test_chain_order_includes_platform_module() -> None:
    text = prompt_service.get_system_prompt()
    names = [*BASE_ORDER, "platform", "style"]
    assert _positions(text, names) == sorted(_positions(text, names))
    assert _marker("multimodal") not in text


def test_multimodal_included_when_requested() -> None:
    text = prompt_service.get_system_prompt(include_multimodal=True)
    assert _marker("multimodal") in text


def test_group_prompt_is_separate() -> None:
    assert "多用户身份规则" in prompt_service.get_group_prompt(member_info="x")


def test_missing_module_raises(tmp_path: Path) -> None:
    (tmp_path / "chat").mkdir()
    (tmp_path / "chat" / "chain.yaml").write_text(
        "version: 1\nmodules:\n  - missing.v1\n", encoding="utf-8"
    )
    with pytest.raises(PromptChainError):
        PromptService(PromptLoader(tmp_path), PromptRenderer())


def test_path_traversal_rejected(tmp_path: Path) -> None:
    (tmp_path / "chat").mkdir()
    (tmp_path / "chat" / "chain.yaml").write_text(
        "modules:\n  - ../secret.v1\n", encoding="utf-8"
    )
    with pytest.raises(PromptChainError):
        PromptService(PromptLoader(tmp_path), PromptRenderer())


def test_invalid_when_rejected(tmp_path: Path) -> None:
    (tmp_path / "chat").mkdir()
    (tmp_path / "chat" / "chain.yaml").write_text(
        "modules:\n  - name: persona\n    when: telegram\n", encoding="utf-8"
    )
    (tmp_path / "chat" / "persona.md").write_text("x", encoding="utf-8")
    with pytest.raises(PromptChainError):
        PromptService(PromptLoader(tmp_path), PromptRenderer())


def test_renderer_substitutes_variables() -> None:
    assert PromptRenderer().render("a {{ x }}", x="b") == "a b"
