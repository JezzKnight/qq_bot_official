"""提示词链路：顺序、版本、平台/多模态条件与配置校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from prompts.loader import PromptLoader
from prompts.renderer import PromptRenderer
from prompts.service import PromptChainError, PromptService, prompt_service

CHAT_DIR = Path("prompts/chat")
SYSTEM_FILE = CHAT_DIR / "system.md"
HEADS = [
    "# 大肥鱼",
    "# 群聊里的相处方式",
    "# 什么时候必须先查",
    "# 不确定的时候",
    "# QQ 群聊限制",
    "# 图片和表情包",
    "# 说话方式",
]


def test_chain_version() -> None:
    assert prompt_service.chain_version == 14
    assert [ref.name for ref in prompt_service.modules] == ["system"]
    assert prompt_service.modules[0].version == "v3"


def test_system_prompt_is_one_file_in_order() -> None:
    text = prompt_service.get_system_prompt()
    positions = [text.index(head) for head in HEADS]
    assert positions == sorted(positions)


def test_system_prompt_has_no_fish_cliche_examples() -> None:
    """鱼八股一节要判据，且不能再把「行 我认」当成正面示范写进认错话术。"""
    text = prompt_service.get_system_prompt()
    assert "# 鱼八股" in text
    assert "认账腔" in text and "点评腔" in text and "判决腔" in text
    assert "“行 我认”“你说得对”“我闭嘴”" not in text


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


def test_persona_uses_sticker_name() -> None:
    text = SYSTEM_FILE.read_text(encoding="utf-8")
    assert "等饭" in text
    assert "`ee`" not in text
