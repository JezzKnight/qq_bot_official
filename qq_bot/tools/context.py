import contextvars
from typing import TypedDict

from qq_bot.ai.types import ImageData

class SearchTracker(TypedDict):
    """子 Agent 搜索过程中累积的 Tavily 调用状态"""

    tavily_success: bool  # 至少一次返回了有效结果
    tavily_error_count: int  # 返回空/失败的次数
    search_rounds: int  # 累计检索轮数（每调用一次 web_search 加一）


current_scope: contextvars.ContextVar[str] = contextvars.ContextVar("current_scope")
current_sender_name: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_sender_name"
)
current_search_tracker: contextvars.ContextVar[SearchTracker | None] = (
    contextvars.ContextVar("current_search_tracker", default=None)
)
# 当前消息携带的图片（handler 下载后写入）：给工具提供"按序号取图"的句柄，
# 否则工具拿不到图——主模型只看到内容，看不到可引用的图片对象（note §61.2 第 3 条）。
current_images: contextvars.ContextVar[list[ImageData] | None] = contextvars.ContextVar(
    "current_images", default=None
)
# 本轮待发送的表情包 (name, bytes)：工具只入队，handler 在文字之后真正发出，
# 保证"先说话再发表情"的顺序（工具执行时文字还没生成完）
current_pending_sticker: contextvars.ContextVar[tuple[str, bytes] | None] = (
    contextvars.ContextVar("current_pending_sticker", default=None)
)
