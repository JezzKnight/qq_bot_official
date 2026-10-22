"""工具注册表的最小回归：防止“工具文件存在但没 import”再次发生。"""

from qq_bot.tools import TOOLS
from qq_bot.tools.registry import get_tools_schema


def test_set_my_name_is_registered() -> None:
    assert "set_my_name" in TOOLS


def test_core_tools_are_registered() -> None:
    for name in ("save_memory", "recall_memory", "search_agent", "send_sticker"):
        assert name in TOOLS


def test_search_schema_only_exposes_query() -> None:
    schema = get_tools_schema("search")[0]["function"]
    assert set(schema["parameters"]["properties"]) == {"query"}
