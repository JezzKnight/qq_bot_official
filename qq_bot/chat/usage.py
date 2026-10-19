"""Token 用量的展示格式化（平台无关）。"""

from __future__ import annotations

from typing import Any


def format_usage(summary: dict[str, int], day: str, today_str: str) -> list[str]:
    """把聚合结果格式化成 /usage 的正文。"""
    prompt = summary["prompt_tokens"]
    cached = summary["cached_tokens"]
    completion = summary["completion_tokens"]
    miss = prompt - cached
    total = prompt + completion

    lines = [f"📊 {day} Token 用量", "────────────────────"]
    if cached:
        lines.append(f"输入(缓存命中): {cached:,} Token")
    if miss:
        lines.append(f"输入(未命中): {miss:,} Token")
    if completion:
        lines.append(f"输出: {completion:,} Token")
    if total:
        lines.append(f"合计: {total:,} Token")
        lines.append(f"调用: {summary['calls']} 次 / {summary['turns']} 轮")
    else:
        label = "今日" if day == today_str else "该日"
        lines.append(f"{label}暂无 API 调用记录")
    return lines


async def memory_stats_line(repo: Any) -> str | None:
    """记忆规模（读 PG；失败只降级，不影响用量输出）。"""
    if repo is None:
        return None
    try:
        stats = await repo.memory_stats()
    except Exception:  # noqa: BLE001 —— 统计失败不应影响用量查询
        return None
    return (
        f"🧠 记忆库：消息 {stats['messages']:,} 条 / "
        f"摘要 {stats['summaries']:,} 条 / 长期记忆 {stats['memories']:,} 条"
    )
