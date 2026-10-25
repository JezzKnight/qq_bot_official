"""后台视频任务：先释放主对话 turn，分析完成后再单独回复。

视频下载、抽帧和视觉理解通常要 15-30 秒，甚至更久。主 turn 不应该为了它
一直占着模型调用链；这里把视频任务放进 asyncio.Task，立刻返回一句“稍等”，
任务完成后用同一事件的消息通道发送结果，并把分析记录写回线程。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qq_bot.ai import usage_context
from qq_bot.ai.types import ChatMessage
from qq_bot.chat.persist import persist_assistant
from qq_bot.chat.session_lock import group_session_key, session_lock
from qq_bot.memory.assembler import trim_messages
from qq_bot.platform.reply import send_reply_chunks, split_for_reply
from qq_bot.services import token_usage
from qq_bot.services.client_factory import api_model_name, get_client_for_model
from qq_bot.services.memory_writing import get_memory
from qq_bot.video.config import VideoConfig
from qq_bot.video.pipeline import VideoAgent

if TYPE_CHECKING:
    from collections.abc import Callable

    from qq_bot.platform.ports import Incoming, MediaRef

logger = logging.getLogger(__name__)

VIDEO_ACK = "我去看看这个视频 稍等一下"

_active_jobs: dict[tuple[int, str], asyncio.Task[None]] = {}


@dataclass(slots=True)
class VideoJob:
    """后台任务需要的全部上下文；不依赖 run_turn 的局部变量或 contextvar。"""

    incoming: Incoming
    thread_id: int
    group_id: str | None
    sender_id: str
    sender_name: str
    user_text: str
    question: str
    source: MediaRef | str
    system_prompt: str
    history: list[ChatMessage]
    model_name: str
    temperature: float
    max_tokens: int
    max_context_tokens: int
    memory: Any
    schedule: Callable[[str], None] | None
    typing_delay: bool = True


def _job_key(thread_id: int, message_id: str) -> tuple[int, str]:
    return thread_id, message_id or "unknown"


def is_video_job_active(thread_id: int, message_id: str) -> bool:
    """同一线程的同一条消息只允许一个后台任务。"""
    return _job_key(thread_id, message_id) in _active_jobs


def start_video_job(job: VideoJob) -> bool:
    """创建后台任务并立即返回；重复消息返回 False。"""
    key = _job_key(job.thread_id, job.incoming.message_id)
    if key in _active_jobs:
        logger.info("后台视频任务已存在，跳过重复创建 key=%s", key)
        return False

    task = asyncio.create_task(_run_video_job(job), name=f"video-job-{key[1]}")
    _active_jobs[key] = task

    def _done(done: asyncio.Task[None]) -> None:
        _active_jobs.pop(key, None)
        if done.cancelled():
            logger.info("后台视频任务已取消 key=%s", key)
            return
        exc = done.exception()
        if exc is not None:
            logger.error("后台视频任务异常 key=%s: %r", key, exc)

    task.add_done_callback(_done)
    logger.info("后台视频任务已启动 key=%s source=%s", key, type(job.source).__name__)
    return True


async def _run_video_job(job: VideoJob) -> None:
    """执行视频分析、生成回答、发送并落库；任何失败都要给用户一个结果。"""
    usage_context.reset()
    note = ""
    answer = ""
    try:
        config = VideoConfig.from_env()
        async with asyncio.timeout(config.job_timeout):
            agent = VideoAgent(
                config=config,
                usage_recorder=token_usage.record,
                note_store=await get_memory(),
            )
            insight = await agent.analyze(job.source, question=job.question)
            note = (insight.text or "").strip()
            answer = await _answer(job, note)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("后台视频任务失败 thread=%s", job.thread_id)
        answer = f"视频没看成：{type(exc).__name__}"
        note = ""

    try:
        await _send_answer(job, answer)
    except Exception:
        logger.exception("后台视频结果发送失败 thread=%s", job.thread_id)

    await persist_assistant(
        job.memory,
        thread_id=job.thread_id,
        group_id=job.group_id,
        assistant_text=answer,
        extra_assistant_text=(
            f"[视频分析记录，仅供后续对话参考]\n{note}" if note else None
        ),
        usage=usage_context.snapshot(),
        model=job.model_name,
        schedule=job.schedule,
    )
    logger.info(
        "后台视频任务完成 thread=%s answer_chars=%d", job.thread_id, len(answer)
    )


async def _answer(job: VideoJob, note: str) -> str:
    """用主模型把视频分析记录整理成给用户的回答；不再走工具循环。"""
    if not note:
        return "视频没看成 稍后再发我一次"

    client = await get_client_for_model(job.model_name)
    system = (
        f"{job.system_prompt}\n\n"
        "## 后台视频回答\n"
        "你已经看完用户刚发来的视频，下面是分析记录。直接回答用户，"
        "只依据记录，不要编造；不要说你没有看过视频；口语化、简短。"
    )
    messages = [ChatMessage(role="system", content=system)]
    messages.extend(job.history)
    question = job.question
    question_block = (
        "" if question and question in job.user_text else f"\n[用户问题]\n{question}"
    )
    messages.append(
        ChatMessage(
            role="user",
            content=(f"{job.user_text}{question_block}\n\n[视频分析记录]\n{note}"),
        )
    )
    messages = trim_messages(messages, job.max_context_tokens)
    response = await client.chat(
        messages=messages,
        model=api_model_name(job.model_name),
        temperature=job.temperature,
        max_tokens=min(job.max_tokens, 1200),
    )
    await token_usage.record(
        response.prompt_tokens,
        response.cached_tokens,
        response.completion_tokens,
    )
    content = (response.content or "").strip()
    return content or "视频看完了 但没整理出能说的内容"


async def _send_answer(job: VideoJob, answer: str) -> None:
    """群聊首条被动回复（带 @），后续分段主动；失败时退回普通消息。"""
    reply = job.incoming.reply
    incoming = job.incoming
    chunks = split_for_reply(answer, max_parts=max(1, reply.max_parts))
    async with session_lock(group_session_key(incoming.group_key, incoming.user_key)):
        await send_reply_chunks(
            reply,
            chunks,
            mention_user_key=incoming.user_key if incoming.is_group else None,
            simulate_typing=job.typing_delay,
            fallback_to_text=True,
        )
        await reply.finish()
