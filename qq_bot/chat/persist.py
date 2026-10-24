"""一轮对话的落库：用户 + assistant，可附一条不发送给用户的工具分析笔记。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


async def persist_user(  # noqa: PLR0913 —— 一行的完整列，拆开会更难读
    memory: Any,
    *,
    thread_id: int | None,
    group_id: str | None,
    sender_id: str,
    sender_name: str,
    user_text: str,
    external_id: str | None = None,
) -> None:
    """只写用户消息；后台任务需要先把用户消息落库，再慢慢生成回复。"""
    if memory is None or thread_id is None:
        return
    try:
        await memory.append(
            thread_id=thread_id,
            group_id=group_id,
            sender_id=sender_id,
            role="user",
            content=user_text,
            sender_name=sender_name,
            external_id=external_id,
        )
    except Exception:
        logger.exception("用户消息落库失败（不影响后续回复）")


async def persist_assistant(  # noqa: PLR0913 —— 一行的完整列，拆开会更难读
    memory: Any,
    *,
    thread_id: int | None,
    group_id: str | None,
    assistant_text: str,
    extra_assistant_text: str | None = None,
    sticker: str | None = None,
    usage: Any | None = None,
    model: str | None = None,
    schedule: Callable[[str], None] | None = None,
) -> None:
    """只写 assistant 消息和可选的分析笔记；用户消息已由 persist_user 写入。"""
    if memory is None or thread_id is None:
        return
    try:
        await memory.append(
            thread_id=thread_id,
            group_id=group_id,
            sender_id=None,
            role="assistant",
            content=assistant_text,
            images=[{"type": "sticker", "name": sticker}] if sticker else None,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=usage.cached_tokens if usage else 0,
            llm_calls=usage.calls if usage else 0,
            model=model,
        )
        if extra_assistant_text:
            # 工具产生的长分析不发送给用户，但必须进入线程，否则下一轮只剩
            # 一句很短的最终回复，模型会“忘记”自己看过什么。
            await memory.append(
                thread_id=thread_id,
                group_id=group_id,
                sender_id=None,
                role="assistant",
                content=extra_assistant_text,
            )
        if group_id is not None and schedule is not None:
            # 摘要压缩异步执行，不进入用户等待路径
            schedule(group_id)
    except Exception:
        logger.exception("助手消息落库失败（不影响本次回复）")


async def persist_turn(  # noqa: PLR0913 —— 一轮对话的完整字段
    memory: Any,
    *,
    thread_id: int | None,
    group_id: str | None,
    sender_id: str,
    sender_name: str,
    user_text: str,
    assistant_text: str,
    extra_assistant_text: str | None = None,
    sticker: str | None = None,
    usage: Any | None = None,
    model: str | None = None,
    schedule: Callable[[str], None] | None = None,
    external_id: str | None = None,
) -> None:
    """落库一轮完整对话；失败只记日志，不影响已经生成的回复。"""
    await persist_user(
        memory,
        thread_id=thread_id,
        group_id=group_id,
        sender_id=sender_id,
        sender_name=sender_name,
        user_text=user_text,
        external_id=external_id,
    )
    await persist_assistant(
        memory,
        thread_id=thread_id,
        group_id=group_id,
        assistant_text=assistant_text,
        extra_assistant_text=extra_assistant_text,
        sticker=sticker,
        usage=usage,
        model=model,
        schedule=schedule,
    )
