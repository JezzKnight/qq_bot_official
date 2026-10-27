#!/usr/bin/env bash
# 与原先的 run_bot.bat 等价：死循环拉起 bot.py，进程退出后等待 3 秒重启。
# 群聊里 /reboot（别名 重启、重启服务）走 os._exit(0) 结束进程，由本循环负责重新拉起。
# 依赖准备：uv sync --frozen（生成 .venv）；用法：./run_bot.sh
set -u

cd "$(dirname "$0")" || { echo "[run_bot] 无法进入项目目录" >&2; exit 1; }

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "[run_bot] 找不到 $PY，请先执行： uv sync --frozen" >&2
    exit 1
fi

# 关掉环境代理：WSL 的 autoProxy 会把国内请求（QQ 图床等）也导向境外代理（见 note §77.5）
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy 2>/dev/null || true

while true; do
    echo "[$(date '+%F %T')] Starting QQ Bot..."
    "$PY" bot.py
    code=$?
    echo "[$(date '+%F %T')] Bot exited (code ${code}), restarting in 3 seconds..."
    sleep 3
done
