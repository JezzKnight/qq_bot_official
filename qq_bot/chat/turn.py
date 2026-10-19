"""平台无关的一轮对话：身份 → 上下文 → 提示词 → 工具循环 → 落库 → 回复。

本模块不 import 任何 nonebot 适配器；平台差异由 Incoming / ReplyTarget 承担。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from prompts.service import prompt_service
from qq_bot.ai import usage_context
from qq_bot.ai.types import ChatMessage
from qq_bot.chat.persist import persist_turn
from qq_bot.chat.prompt import SystemPromptParts, build_system_prompt
from qq_bot.media.download import download_image
from qq_bot.memory.assembler import ContextParts, build_context, trim_messages
from qq_bot.memory.members import members_xml
from qq_bot.memory.memory_config import get_memory_config
from qq_bot.memory.naming import escape_for_prompt
from qq_bot.platform.reply import split_for_reply
from qq_bot.services import token_usage
from qq_bot.services.client_factory import (
    default_model_name,
    get_client_for_model,
    get_image_edit_spec,
    get_model_params,
)
from qq_bot.services.dynamic_injection import (
    build_memory_injection,
    build_private_injection,
)
from qq_bot.services.memory_compress import schedule_compress
from qq_bot.services.memory_writing import get_memory
from qq_bot.services.session_store import get_session_model
from qq_bot.services.sticker_sync import catalog_text
from qq_bot.tools import (
    TOOLS,
    SearchTracker,
    current_edited_images,
    current_images,
    current_interaction,
    current_pending_sticker,
    current_scope,
    current_search_tracker,
    current_send_msg,
    current_sender_name,
    get_tools_schema,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from qq_bot.platform.ports import Incoming

logger = logging.getLogger(__name__)


def _compression_scheduler(incoming: Incoming):
    """看不到全量群消息时不做群摘要：否则摘要只覆盖 @ 对话，质量太差。"""
    return schedule_compress if incoming.caps.can_see_all_group_messages else None

MAX_TOOL_ROUNDS = 5
FALLBACK_TEXT = "AI暂时无法响应，请稍后重试"


async def _download_images(incoming: Incoming) -> list[Any]:
    images: list[Any] = []
    for ref in incoming.images:
        if ref.kind == "image" and ref.url:
            img = await download_image(ref.url)
            if img is not None:
                images.append(img)
    return images


async def _send_pending_sticker(reply: Any, pending: tuple[str, bytes] | None) -> None:
    if pending is None:
        return
    name, data = pending
    try:
        await reply.image(data, name)
    except Exception:
        logger.exception("表情包发送失败 name=%s", name)


async def run_turn(  # noqa: C901, PLR0912, PLR0915 —— 迁移自 handler 的主流程
    incoming: Incoming,
    *,
    config: Any,
    bootstrap_members: Callable[[], Awaitable[bool]] | None = None,
) -> str:
    """执行一轮对话并返回最终回复文本。"""
    sender_name = incoming.display_name or "未知用户"
    is_group = incoming.is_group
    memory = await get_memory()

    if memory is None:
        return "记忆库暂时不可用 稍后再试"
    uid = incoming.user_key
    gid = incoming.group_key

    scope = f"groups/{gid}/{uid}" if is_group else f"private/{uid}"
    current_scope.set(scope)
    current_sender_name.set(sender_name)
    current_send_msg.set(incoming.reply.text)
    current_pending_sticker.set(None)
    usage_context.reset()

    session_id = f"group_{gid}" if is_group else f"user_{uid}"
    model_name = get_session_model(session_id, default_model_name())
    model_params = get_model_params(model_name)

    thread_id: int | None = None
    parts = ContextParts()
    if memory is not None:
        thread_id = await memory.ensure_thread(
            kind="group_member" if is_group else "private",
            group_id=gid,
            user_id=uid,
        )
        parts = await build_context(
            memory,
            thread_id=thread_id,
            group_id=gid,
            config=get_memory_config(),
        )
        if is_group and gid is not None and (incoming.card or incoming.nickname):
            try:
                await memory.refresh_member_name(
                    group_id=gid,
                    user_id=uid,
                    card=incoming.card or "",
                    nickname=incoming.nickname or "",
                )
            except Exception:
                logger.debug("成员名片刷新失败", exc_info=True)

    system_part = prompt_service.get_system_prompt()
    if is_group:
        member_info = await members_xml(memory, gid) if memory is not None else None
        if not member_info and bootstrap_members is not None:  # noqa: SIM102
            if await bootstrap_members():
                member_info = (
                    await members_xml(memory, gid) if memory is not None else None
                )
        if not member_info:
            member_info = "暂无群成员信息"
        group_part = prompt_service.get_group_prompt(member_info=member_info)
        memory_prompt = None
        if config.memory_injection_enabled:
            try:
                memory_prompt, _ = await build_memory_injection(
                    gid,
                    uid,
                    incoming.text,
                    self_id=incoming.self_key or "",
                    glossary_enabled=config.glossary_enabled,
                )
            except Exception as exc:  # noqa: BLE001 —— 注入失败只降级
                logger.warning("动态记忆注入失败，降级为不注入: %s", type(exc).__name__)
    else:
        group_part = f"你当前正在与 {sender_name} 私聊。"
        memory_prompt = None
        if config.memory_injection_enabled:
            try:
                memory_prompt = await build_private_injection(uid)
            except Exception as exc:  # noqa: BLE001 —— 注入失败只降级
                logger.warning("私聊记忆注入失败，降级为不注入: %s", type(exc).__name__)

    sticker_catalog = catalog_text() or ""
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y年%m月%d日 %H:%M:%S")
    final_prompt = build_system_prompt(
        SystemPromptParts(
            system=system_part,
            group=group_part,
            sticker=sticker_catalog,
            summary=(
                f"## 群聊摘要（更早的对话，仅供参考）\n{parts.summary}"
                if parts.summary
                else ""
            ),
            speaker=f'当前发言人: uid="{uid}", name="{sender_name}"',
            memory=(
                f"以下是本轮对话动态注入的长期记忆：\n{memory_prompt}"
                if memory_prompt
                else ""
            ),
            time=f"当前对话发生的时间为{now} (UTC+8)，以此时间为准",
        )
    )

    messages = [ChatMessage(role="system", content=final_prompt)]
    messages.extend(parts.as_history())

    images = await _download_images(incoming)
    current_images.set(images)
    video_urls = [
        ref.url for ref in incoming.images if ref.kind == "video" and ref.url
    ]
    current_call = (
        await memory.preferred_name(uid) if memory is not None else None
    )
    call_attr = f' call="{escape_for_prompt(current_call)}"' if current_call else ""
    identity_tag = f'<user identity id="{uid}" name="{sender_name}"{call_attr}/>'
    messages.append(
        ChatMessage(
            role="user",
            content=f"{identity_tag}\n{incoming.text}",
            sender_name=sender_name,
            images=images or None,
            media_urls=video_urls or None,
        )
    )
    messages = trim_messages(messages, config.max_context_tokens)

    tool_names = [
        "search_agent",
        "save_memory",
        "save_glossary",
        "recall_memory",
        "query_chat_history",
        "set_my_name",
    ]
    if incoming.caps.can_active_message:
        tool_names.extend(["schedule_reminder", "cancel_reminder"])
    # 没有表情目录（如 QQ 模式未同步）或平台不能发图时不暴露相关工具
    if sticker_catalog and incoming.caps.can_send_image:
        tool_names.append("send_sticker")
    if is_group and incoming.caps.can_active_message:
        tool_names.append("interact_with_member")
    if get_image_edit_spec().enabled and incoming.caps.can_send_image:
        tool_names.append("edit_image")

    client = await get_client_for_model(model_name)
    search_agent_called = False
    final_content = FALLBACK_TEXT
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat(
            messages=messages,
            model=model_name,
            temperature=model_params.temperature,
            max_tokens=model_params.max_tokens,
            tools=get_tools_schema(*tool_names),
        )
        await token_usage.record(
            response.prompt_tokens,
            response.cached_tokens,
            response.completion_tokens,
        )
        if not response.tool_calls:
            final_content = response.content or "AI暂时无法响应"
            break

        messages.append(
            ChatMessage(
                role="assistant",
                tool_calls=response.tool_calls,
                reasoning_content=response.reasoning_content,
            )
        )
        for tc in response.tool_calls:
            func = tc["function"]
            tool = TOOLS[func["name"]]
            args = json.loads(func["arguments"])
            if func["name"] == "search_agent":
                if search_agent_called:
                    result = "[系统提示] 本轮已搜索过，直接基于现有信息回答。"
                else:
                    search_agent_called = True
                    args = {
                        "model": model_name,
                        "task": args.get("task", incoming.text),
                    }
                    tracker: SearchTracker = {
                        "tavily_success": False,
                        "tavily_error_count": 0,
                        "search_rounds": 0,
                    }
                    current_search_tracker.set(tracker)
                    try:
                        if incoming.caps.can_progress_messages:
                            await incoming.reply.text("正在找寻相关信息")
                        search_start = time.monotonic()
                        result = await tool["func"](**args)
                        search_time = time.monotonic() - search_start
                    finally:
                        current_search_tracker.set(None)
                    if (
                        incoming.caps.can_progress_messages
                        and tracker["tavily_error_count"] > 0
                        and not tracker["tavily_success"]
                    ):
                        await incoming.reply.text(
                            f"⚠️ Tavily 异常，失败 {tracker['tavily_error_count']} 次"
                        )
                    if (
                        incoming.caps.can_progress_messages
                        and tracker["search_rounds"] > 0
                    ):
                        rounds = tracker["search_rounds"]
                        await incoming.reply.text(
                            f"已完成检索，共执行 {rounds} 轮，"
                            f"耗时 {search_time:.1f} 秒"
                        )
            else:
                result = await tool["func"](**args)

            messages.append(
                ChatMessage(role="tool", content=result, tool_call_id=tc["id"])
            )
        if current_interaction.get() is not None:
            break
    else:
        final_content = FALLBACK_TEXT

    interaction = current_interaction.get()
    if interaction is not None:
        opener = interaction["opener_text"]
        await persist_turn(
            memory,
            thread_id=thread_id,
            group_id=gid,
            sender_id=uid,
            sender_name=sender_name,
            user_text=f"{identity_tag}\n{incoming.text}",
            assistant_text=opener,
            usage=usage_context.snapshot(),
            model=model_name,
            schedule=_compression_scheduler(incoming),
        )
        current_interaction.set(None)
        await incoming.reply.mention(str(interaction["target_uid"]), opener)
        await incoming.reply.finish()

    edited_images = current_edited_images.get()
    current_edited_images.set(None)
    if edited_images:
        for img in edited_images:
            await incoming.reply.image(img.data)
        if not final_content.strip() or final_content.startswith("AI暂时无法响应"):
            final_content = "图已经改好了，看看效果吧"

    pending_sticker = current_pending_sticker.get()
    current_pending_sticker.set(None)
    await persist_turn(
        memory,
        thread_id=thread_id,
        group_id=gid,
        sender_id=uid,
        sender_name=sender_name,
        user_text=f"{identity_tag}\n{incoming.text}",
        assistant_text=final_content,
        usage=usage_context.snapshot(),
        model=model_name,
        sticker=pending_sticker[0] if pending_sticker is not None else None,
        schedule=_compression_scheduler(incoming),
    )

    if is_group:
        max_parts = incoming.reply.max_parts
        if pending_sticker is not None and max_parts > 1:
            # 给表情包留一次发送额度
            max_parts -= 1
        chunks = split_for_reply(final_content, max_parts=max_parts)
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                await incoming.reply.mention(incoming.user_key, chunk)
            else:
                await incoming.reply.text(chunk)
        await _send_pending_sticker(incoming.reply, pending_sticker)
        await incoming.reply.finish()
    else:
        if final_content.strip():
            await incoming.reply.text(final_content)
        await _send_pending_sticker(incoming.reply, pending_sticker)
        await incoming.reply.finish()
    return final_content
