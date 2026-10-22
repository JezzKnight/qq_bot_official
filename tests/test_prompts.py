"""提示词链路：顺序、版本、平台/多模态条件与配置校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from prompts.loader import PromptLoader
from prompts.renderer import PromptRenderer
from prompts.service import PromptChainError, PromptService, prompt_service

CHAT_DIR = Path("prompts/chat")
BASE_ORDER = ["persona", "social", "constraints", "research", "platform", "multimodal"]


def _marker(name: str) -> str:
    content = (CHAT_DIR / f"{name}.md").read_text(encoding="utf-8")
    return content.strip().splitlines()[0][:24]


def _positions(text: str, names: list[str]) -> list[int]:
    return [text.index(_marker(name)) for name in names]


def test_chain_version() -> None:
    assert prompt_service.chain_version == 5
    assert all(ref.version.startswith("v") for ref in prompt_service.modules)


def test_chain_order_includes_all_modules() -> None:
    text = prompt_service.get_system_prompt()
    names = [*BASE_ORDER, "style"]
    assert _positions(text, names) == sorted(_positions(text, names))


def test_group_prompt_is_separate() -> None:
    assert "群聊身份" in prompt_service.get_group_prompt(member_info="x")


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


def test_renderer_substitutes_variables() -> None:
    assert PromptRenderer().render("a {{ x }}", x="b") == "a b"


def test_research_prompt_is_loaded() -> None:
    text = prompt_service.get_system_prompt()
    assert "League of Heroes" in text
    assert "不要默认" in text


def test_persona_uses_sticker_name() -> None:
    text = (CHAT_DIR / "persona.md").read_text(encoding="utf-8")
    assert "等饭" in text
    assert "`ee`" not in text


def test_research_has_no_source_footer() -> None:
    text = (CHAT_DIR / "research.md").read_text(encoding="utf-8")
    assert "末尾给一条最相关来源" not in text
