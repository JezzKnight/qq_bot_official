"""工具注册入口：导入即触发 register_tool，并聚合导出全部工具与上下文符号。"""
from .batch_search import batch_search
from .context import (
    SearchTracker,
    current_images,
    current_pending_sticker,
    current_scope,
    current_search_tracker,
    current_sender_name,
)
from .query_chat_history import query_chat_history
from .recall_memory import recall_memory
from .registry import TOOLS, get_tools_schema, register_tool
from .save_glossary import save_glossary
from .save_memory import save_memory
from .search_agent_tool import search_agent
from .search_tool import search
from .send_sticker import send_sticker
from .set_my_name import set_my_name
from .web_fetch import web_fetch_by_tavily
from .web_search import web_search_by_tavily

__all__ = [
    "TOOLS",
    "SearchTracker",
    "batch_search",
    "current_images",
    "current_pending_sticker",
    "current_scope",
    "current_search_tracker",
    "current_sender_name",
    "get_tools_schema",
    "query_chat_history",
    "recall_memory",
    "register_tool",
    "save_glossary",
    "save_memory",
    "search",
    "search_agent",
    "send_sticker",
    "set_my_name",
    "web_fetch_by_tavily",
    "web_search_by_tavily",
]
