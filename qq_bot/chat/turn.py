"""平台无关的一轮对话：身份 → 上下文 → 提示词 → 工具循环 → 落库 → 回复。

本模块不 import 任何 nonebot 适配器；平台差异由 Incoming / ReplyTarget 承担。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from prompts.service import prompt_service
from qq_bot.ai import usage_context
from qq_bot.ai.types import ChatMessage
from qq_bot.chat.persist import persist_turn, persist_user
from qq_bot.chat.prompt import SystemPromptParts, build_system_prompt
from qq_bot.chat.query_mode import is_query_question
from qq_bot.chat.session_lock import group_session_key, session_lock
from qq_bot.chat.video_background import (
    VIDEO_ACK,
    VideoJob,
    is_video_job_active,
    start_video_job,
)
from qq_bot.media.download import download_image
from qq_bot.memory.assembler import ContextParts, build_context, trim_messages
from qq_bot.memory.members import members_xml
from qq_bot.memory.memory_config import get_memory_config
from qq_bot.memory.naming import escape_for_prompt
from qq_bot.platform.reply import send_reply_chunks, split_for_reply, typing_delay
from qq_bot.search.retry import RETRY_ACK, looks_insufficient
from qq_bot.services import token_usage
from qq_bot.services.client_factory import (
    api_model_name,
    default_model_name,
    get_client_for_model,
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
    current_images,
    current_pending_sticker,
    current_scope,
    current_search_tracker,
    current_sender_name,
    current_videos,
    get_tools_schema,
)
from qq_bot.tools.search_tool import retry_search

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from qq_bot.platform.ports import Incoming

logger = logging.getLogger(__name__)


def _compression_scheduler(incoming: Incoming):
    """看不到全量群消息时不做群摘要：否则摘要只覆盖 @ 对话，质量太差。"""
    return schedule_compress if incoming.caps.can_see_all_group_messages else None


MAX_TOOL_ROUNDS = 5
TOOL_TRACE_CHARS = 800
FALLBACK_TEXT = "AI暂时无法响应，请稍后重试"


async def _download_images(incoming: Incoming) -> list[Any]:
    images: list[Any] = []
    for ref in incoming.images:
        if ref.kind == "image" and ref.url:
            img = await download_image(ref.url)
            if img is not None:
                images.append(img)
    return images


_NOTE_PREFIXES = ("[检索记录]", "[视频分析记录")


def _clip_text(text: str, limit: int) -> str:
    """落库的工具输出裁剪，控制进上下文的历史体积。"""
    value = (text or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _strip_echoed_notes(text: str) -> str:
    """模型有时会把系统台账当成自己的发言复述出来；发送前剥掉开头这段。"""
    cleaned = (text or "").lstrip()
    while cleaned.startswith(_NOTE_PREFIXES):
        idx = cleaned.find("\n\n")
        cleaned = "" if idx < 0 else cleaned[idx + 2 :].lstrip()
    return cleaned


async def _send_pending_sticker(reply: Any, pending: tuple[str, bytes] | None) -> None:
    if pending is None:
        return
    name, data = pending
    try:
        await reply.image(data, name)
    except Exception:
        logger.exception("表情包发送失败 name=%s", name)


async def _reply_video_ack(reply: Any) -> None:
    """先发“稍等”再释放 turn；被动通道能保证它排在最终回答前面。"""
    try:
        await reply.passive(VIDEO_ACK)
    except Exception:
        logger.warning("视频处理提示被动发送失败，改走主动消息", exc_info=True)
        try:
            await reply.text(VIDEO_ACK)
        except Exception:
            logger.exception("视频处理提示发送失败，后台任务继续")



def _annotate_history(history: list[ChatMessage]) -> list[ChatMessage]:
    """给群历史里的用户消息补发送者标签。

    只影响拼给模型的 prompt，不落库；兼容已被标注过的旧数据。
    """
    annotated: list[ChatMessage] = []
    for message in history:
        if (
            message.role != "user"
            or not message.content
            or message.content.startswith("<user identity")
        ):
            annotated.append(message)
            continue
        name = escape_for_prompt(message.sender_name or "某成员")
        uid = escape_for_prompt(message.sender_id or "")
        identity = (
            f'<user identity name="{name}" id="{uid}"/>'
            if uid
            else f'<user identity name="{name}"/>'
        )
        annotated.append(
            replace(message, content=f"{identity}\n{message.content}")
        )
    return annotated


async def run_turn(incoming: Incoming, *, config: Any) -> str:
    """执行一轮对话；同一会话内串行，避免两轮回复的分段消息在群里交叉。"""
    async with session_lock(group_session_key(incoming.group_key, incoming.user_key)):
        return await _run_turn(incoming, config=config)


async def _send_markdown_parts(
    send: Callable[[str], Awaitable[None]],
    content: str,
    *,
    max_parts: int,
    simulate_typing: bool = True,
) -> int:
    """按空行把 Markdown 拆成多条消息；含代码块时不拆，避免破坏格式。"""
    if "```" in content:
        parts = [content]
    else:
        parts = split_for_reply(content, max_parts=max_parts)
    for index, part in enumerate(parts):
        if index and simulate_typing:
            await asyncio.sleep(typing_delay(part))
        await send(part)
    return len(parts)


async def _run_turn(  # noqa: C901, PLR0912, PLR0915 —— 迁移自 handler 的主流程
    incoming: Incoming,
    *,
    config: Any,
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
    current_pending_sticker.set(None)
    current_videos.set([])
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
            exclude_external_id=incoming.message_id,
        )

    system_part = prompt_service.get_system_prompt()
    mode_part = ""
    if is_query_question(incoming.text):
        mode_part = prompt_service.get_query_prompt()
        logger.info("TURN query mode uid=%s", uid)
    if is_group:
        member_info = await members_xml(memory, gid) if memory is not None else None
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
            mode=mode_part,
            sticker=sticker_catalog,
            summary=(
                f"## 群聊摘要（更早的对话）\n{parts.summary}"
                if parts.summary
                else ""
            ),
            speaker=(
                f'当前发言人: uid="{uid}", name="{sender_name}"'
                + (f"\n{incoming.hint}" if incoming.hint else "")
            ),
            memory=(
                f"以下是本轮对话动态注入的长期记忆：\n{memory_prompt}"
                if memory_prompt
                else ""
            ),
            records=(
                "## 系统台账（你自己查过的记录；不要复述，也不要模仿这个格式）\n"
                f"{parts.notes}"
                if parts.notes
                else ""
            ),
            time=f"当前对话发生的时间为{now} (UTC+8)，以此时间为准",
        )
    )

    history = parts.as_history()
    if is_group:
        history = _annotate_history(history)
    messages = [ChatMessage(role="system", content=final_prompt)]
    messages.extend(history)

    images = await _download_images(incoming)
    current_images.set(images)
    # 视频不再塞进 media_urls（那会被模型当成 image_url 发出去）；
    # 改成工具上下文 + 一句提示，由主模型决定是否调用 watch_video。
    video_refs = [ref for ref in incoming.images if ref.kind == "video" and ref.url]
    current_videos.set(video_refs)
    current_call = await memory.preferred_name(uid) if memory is not None else None
    call_attr = f' call="{escape_for_prompt(current_call)}"' if current_call else ""
    identity_tag = f'<user identity id="{uid}" name="{sender_name}"{call_attr}/>'
    user_content = f"{identity_tag}\n{incoming.text}"
    if video_refs:
        user_content += (
            "\n[用户发送了视频或视频链接；需要理解视频内容时调用 watch_video 工具]"
        )

    if video_refs and getattr(config, "video_background_enabled", True):
        # 视频分析不占用主 turn：先落用户消息、发 ack，再把分析交给后台任务。
        if not is_video_job_active(thread_id or 0, incoming.message_id):
            await _reply_video_ack(incoming.reply)
            await persist_user(
                memory,
                thread_id=thread_id,
                group_id=gid,
                sender_id=uid,
                sender_name=sender_name,
                user_text=f"{identity_tag}\n{incoming.text}",
                external_id=incoming.message_id,
            )
            question = incoming.text.strip() or "请总结这个视频的内容"
            start_video_job(
                VideoJob(
                    incoming=incoming,
                    thread_id=thread_id or 0,
                    group_id=gid,
                    sender_id=uid,
                    sender_name=sender_name,
                    user_text=f"{identity_tag}\n{incoming.text}",
                    question=question,
                    source=video_refs[0],
                    system_prompt=final_prompt,
                    history=parts.as_history(),
                    model_name=model_name,
                    temperature=model_params.temperature,
                    max_tokens=model_params.max_tokens,
                    max_context_tokens=config.max_context_tokens,
                    memory=memory,
                    schedule=_compression_scheduler(incoming),
                    typing_delay=getattr(config, "reply_typing_delay", True),
                )
            )
        return ""

    messages.append(
        ChatMessage(
            role="user",
            content=user_content,
            sender_name=sender_name,
            images=images or None,
        )
    )
    messages = trim_messages(messages, config.max_context_tokens)

    tool_names = [
        "search",
        "save_memory",
        "save_glossary",
        "recall_memory",
        "query_chat_history",
        "set_my_name",
    ]
    if video_refs:
        tool_names.append("watch_video")
    # 没有表情目录（如 QQ 模式未同步）或平台不能发图时不暴露相关工具
    if sticker_catalog and incoming.caps.can_send_image:
        tool_names.append("send_sticker")

    client = await get_client_for_model(model_name)
    search_called = False
    search_retried = False
    video_ack_sent = False
    video_notes: list[str] = []
    last_search_query = ""
    reply_parts_used = 0
    final_content = FALLBACK_TEXT
    tool_trace: list[ChatMessage] = []
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat(
            messages=messages,
            model=api_model_name(model_name),
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
            answer = response.content or ""
            if (
                not search_retried
                and search_called
                and last_search_query
                and looks_insufficient(answer)
            ):
                search_retried = True
                try:
                    # 走被动回复通道：补偿消息和最终回复同一通道，QQ 不会因为
                    # 主动消息排队而让最终回复先到；不能用 text()（主动消息）。
                    await incoming.reply.passive(RETRY_ACK)
                    reply_parts_used += 1
                except Exception:
                    logger.warning("补查提示发送失败，继续补查", exc_info=True)

                try:
                    retry_context = await retry_search(last_search_query)
                except Exception:
                    logger.exception("搜索补查异常，保留原回答")
                    retry_context = ""
                if retry_context:
                    messages.append(
                        ChatMessage(
                            role="assistant",
                            content=answer,
                            reasoning_content=response.reasoning_content,
                        )
                    )
                    messages.append(
                        ChatMessage(
                            role="user",
                            content=(
                                "（系统补查）上一轮检索结果不足，已经换关键词重新搜索。"
                                "请只依据下面的新证据重新回答；如果新证据仍然不足，"
                                "再明确说没查到。不要复述补查过程。\n"
                                f"{retry_context}"
                            ),
                        )
                    )
                    continue
            final_content = _strip_echoed_notes(answer) or "AI暂时无法响应"
            break

        messages.append(
            ChatMessage(
                role="assistant",
                tool_calls=response.tool_calls,
                reasoning_content=response.reasoning_content,
            )
        )
        tool_trace.append(
            ChatMessage(
                role="assistant",
                tool_calls=[
                    {
                        **tc,
                        "function": {
                            **tc["function"],
                            "arguments": _clip_text(tc["function"]["arguments"], 400),
                        },
                    }
                    for tc in response.tool_calls
                ],
            )
        )
        for tc in response.tool_calls:
            func = tc["function"]
            tool = TOOLS[func["name"]]
            args = json.loads(func["arguments"])
            if func["name"] == "search":
                if search_called:
                    result = "[系统提示] 本轮已搜索过，直接基于现有信息回答。"
                else:
                    search_called = True
                    last_search_query = str(args.get("query") or incoming.text)
                    tracker: SearchTracker = {
                        "search_success": False,
                        "search_error_count": 0,
                        "search_rounds": 0,
                    }
                    current_search_tracker.set(tracker)
                    try:
                        result = await tool["func"](**args)
                    finally:
                        current_search_tracker.set(None)
            else:
                if func["name"] == "watch_video" and not video_ack_sent:
                    video_ack_sent = True
                    try:
                        await incoming.reply.text("我去看看这个视频 稍等一下")
                        reply_parts_used += 1
                    except Exception:
                        logger.warning("视频处理提示发送失败，继续分析", exc_info=True)
                result = await tool["func"](**args)
                if (
                    func["name"] == "watch_video"
                    and result
                    and not result.startswith(("视频获取失败", "视频抽帧失败"))
                ):
                    video_notes.append(result)

            messages.append(
                ChatMessage(role="tool", content=result, tool_call_id=tc["id"])
            )
            tool_trace.append(
                ChatMessage(
                    role="tool",
                    content=_clip_text(result, TOOL_TRACE_CHARS),
                    tool_call_id=tc["id"],
                )
            )
    else:
        final_content = FALLBACK_TEXT

    pending_sticker = current_pending_sticker.get()
    current_pending_sticker.set(None)
    video_note = "\n\n".join(video_notes)
    if video_note:
        logger.info("TURN video note chars=%d", len(video_note))
    logger.info("TURN persist start")
    await persist_turn(
        memory,
        thread_id=thread_id,
        group_id=gid,
        sender_id=uid,
        sender_name=sender_name,
        user_text=f"{identity_tag}\n{incoming.text}",
        assistant_text=final_content,
        extra_assistant_text=None,
        tool_trace=tool_trace,
        usage=usage_context.snapshot(),
        model=model_name,
        sticker=pending_sticker[0] if pending_sticker is not None else None,
        schedule=_compression_scheduler(incoming),
        external_id=incoming.message_id,
    )
    logger.info("TURN persist done")

    max_parts = max(1, incoming.reply.max_parts - reply_parts_used)
    if pending_sticker is not None and max_parts > 1:
        # 给表情包留一次发送额度
        max_parts -= 1

    markdown_sent = False
    markdown = getattr(incoming.reply, "markdown", None)
    if (
        mode_part
        and markdown is not None
        and getattr(config, "query_markdown_enabled", True)
    ):
        try:
            sent = await _send_markdown_parts(
                markdown,
                final_content,
                max_parts=max_parts,
                simulate_typing=getattr(config, "reply_typing_delay", True),
            )
            markdown_sent = True
            logger.info(
                "TURN markdown sent parts=%d chars=%d", sent, len(final_content)
            )
        except Exception:  # 发送失败只降级，不影响本轮
            logger.warning("查询模式 Markdown 发送失败，回退文本分段", exc_info=True)

    if not markdown_sent:
        chunks = split_for_reply(final_content, max_parts=max_parts)
        await send_reply_chunks(
            incoming.reply,
            chunks,
            mention_user_key=incoming.user_key if is_group else None,
            simulate_typing=getattr(config, "reply_typing_delay", True),
        )
    await _send_pending_sticker(incoming.reply, pending_sticker)
    await incoming.reply.finish()
    return final_content
