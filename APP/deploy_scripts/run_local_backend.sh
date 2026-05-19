#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/network_env.sh"

CONDA_ENV_PATH="${CONDA_ENV_PATH:-/Volumes/MacMini/opt/miniconda3/envs/antigravity}"
PYTHON_BIN="${PYTHON_BIN:-$CONDA_ENV_PATH/bin/python3.12}"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "python 不存在: $PYTHON_BIN" >&2
    exit 1
fi

mkdir -p "$PROJECT_ROOT/APP/backend/logs"
cd "$PROJECT_ROOT/APP/backend"

export HOME="${HOME:-/Users/$(id -un)}"
export PATH="${CONDA_ENV_PATH}/bin:${HOME}/.homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONHOME="${CONDA_ENV_PATH}"
export PYTHONPATH="${PWD}:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1
export JIRA_SKIP_COOKIES="${JIRA_SKIP_COOKIES:-true}"
export ENABLE_CACHE_SERVICE="${ENABLE_CACHE_SERVICE:-false}"
export AITICKET_ROLE="${AITICKET_ROLE:-mini}"
export CRON_HOST="${CRON_HOST:-false}"
# 铁律 9：全局 jira_service 兜底仅允许白名单用户（此机器 = qiangxiao）
export JIRA_GLOBAL_FALLBACK_USERS="${JIRA_GLOBAL_FALLBACK_USERS:-qiangxiao}"

export no_proxy="localhost,127.0.0.1,0.0.0.0,::1"
export NO_PROXY="$no_proxy"

# 启动前清掉残留监听，防止 launchd KeepAlive 重启时 Errno 48
LSOF_PORT="${LOCAL_BACKEND_PORT:-3000}"
if lsof -ti:"$LSOF_PORT" >/dev/null 2>&1; then
    echo "[run_local_backend] port $LSOF_PORT in use, killing stale processes" >&2
    lsof -ti:"$LSOF_PORT" | xargs kill -TERM 2>/dev/null || true
    sleep 1
    lsof -ti:"$LSOF_PORT" | xargs kill -KILL 2>/dev/null || true
fi

exec "$PYTHON_BIN" -m uvicorn main:app \
    --host "$LOCAL_BACKEND_HOST" \
    --port "$LOCAL_BACKEND_PORT" \
    --log-level info \
    --workers 1 \
    --timeout-keep-alive 15 \
    --limit-concurrency 64
