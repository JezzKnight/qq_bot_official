"""提示词服务：按 chat/chain.yaml 的顺序拼装系统提示词。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .loader import PromptLoader
from .renderer import PromptRenderer

logger = logging.getLogger(__name__)

CHAIN_PATH = "chat/chain.yaml"
_VERSION_PATTERN = re.compile(r"^v\d+$")


class PromptChainError(Exception):
    """提示词链路配置不合法（缺文件、格式错误等）。"""


@dataclass(frozen=True)
class ModuleRef:
    """链路里的一个模块：name 对应 chat/{name}.md，version 仅作标记。"""

    name: str
    version: str
    file: str


class PromptService:
    """业务代码与提示词系统之间的统一接口。"""

    def __init__(
        self,
        loader: PromptLoader,
        renderer: PromptRenderer,
        chain_path: str = CHAIN_PATH,
    ) -> None:
        self._loader = loader
        self._renderer = renderer
        self._chain_path = chain_path
        self._chain_version, self._modules = self._load_chain()

    @property
    def chain_version(self) -> Any:
        return self._chain_version

    @property
    def modules(self) -> tuple[ModuleRef, ...]:
        return self._modules

    def _load_chain(self) -> tuple[Any, tuple[ModuleRef, ...]]:
        chain_file = self._loader.base_dir / self._chain_path
        try:
            data = yaml.safe_load(chain_file.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise PromptChainError(f"缺少提示词链路配置: {chain_file}") from exc  # noqa: TRY003

        raw_modules = data.get("modules")
        if not isinstance(raw_modules, list) or not raw_modules:
            raise PromptChainError(f"{chain_file} 的 modules 必须是非空列表")  # noqa: TRY003

        refs: list[ModuleRef] = []
        for raw in raw_modules:
            if not isinstance(raw, str) or not raw.strip():
                raise PromptChainError(  # noqa: TRY003
                    f"{chain_file} 的 modules 条目必须是非空字符串: {raw!r}"
                )
            name, version = self._parse_ref(raw.strip())
            # 只允许 chat/ 下的单层文件名，避免 ../ 之类的路径穿越
            if not name or Path(name).name != name:
                raise PromptChainError(f"非法的模块名: {raw!r}")  # noqa: TRY003
            file = f"chat/{name}.md"
            if not (self._loader.base_dir / file).is_file():
                raise PromptChainError(f"模块 {raw} 对应的文件不存在: {file}")  # noqa: TRY003
            refs.append(ModuleRef(name=name, version=version, file=file))
        return data.get("version", 1), tuple(refs)

    @staticmethod
    def _parse_ref(raw: str) -> tuple[str, str]:
        """把 persona.v1 拆成 ("persona", "v1")；没有合法版本后缀时整串当名字。"""
        name, _, version = raw.rpartition(".")
        if name and _VERSION_PATTERN.match(version):
            return name, version
        return raw, ""

    def get_system_prompt(self, **variables: str) -> str:
        """按链路顺序拼装系统提示词（QQ 专用，所有模块恒定加载）。"""
        parts: list[str] = []
        for ref in self._modules:
            content = self._loader.load(ref.file)
            if content.strip():
                parts.append(content)
        return self._renderer.render("\n\n".join(parts), **variables)

    def get_group_prompt(self, **variables: str) -> str:
        """群聊身份规则提示词（chat/group.md），不参与系统提示词链路。"""
        template = self._loader.load("chat/group.md")
        return self._renderer.render(template, **variables)

    def get_query_prompt(self, **variables: str) -> str:
        """查询/攻略模式提示词（chat/query.md），按需追加。"""
        template = self._loader.load("chat/query.md")
        return self._renderer.render(template, **variables)

    def get_agent_prompt(self, agent_name: str, **variables: str) -> str:
        """子 Agent 提示词（agents/{agent_name}.md）。"""
        template = self._loader.load(f"agents/{agent_name}.md")
        return self._renderer.render(template, **variables)


# 模块级单例，基于 prompts 包所在目录
_base_dir = Path(__file__).parent
prompt_service = PromptService(
    loader=PromptLoader(_base_dir),
    renderer=PromptRenderer(),
)
