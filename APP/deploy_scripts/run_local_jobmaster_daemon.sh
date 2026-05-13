#!/bin/bash
# jobmaster daemon：调度器 + pm_scheduler + automation + KB 写队列消费
# 只运行一个实例（launchd KeepAlive，单进程）
# API workers 不设 RUN_BACKGROUND_JOBS，不重复触发 cron。

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
export RUN_BACKGROUND_JOBS=1
export JIRA_SKIP_COOKIES="${JIRA_SKIP_COOKIES:-true}"
export ENABLE_CACHE_SERVICE="${ENABLE_CACHE_SERVICE:-false}"
# Chroma server mode（切到 server 模式时解注释）
# export CHROMA_MODE=server
# export CHROMA_HOST=127.0.0.1
# export CHROMA_KB_PORT=8001
# export CHROMA_BOARD_PORT=8002

unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy
export no_proxy="localhost,127.0.0.1,0.0.0.0,::1"
export NO_PROXY="$no_proxy"

exec "$PYTHON_BIN" -m scripts.local_jobmaster_daemon
