#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/network_env.sh"

CONDA_ENV_PATH="${CONDA_ENV_PATH:-/Volumes/MacMini/opt/miniconda3/envs/antigravity}"
PYTHON_BIN="${PYTHON_BIN:-$CONDA_ENV_PATH/bin/python3.12}"

if [ ! -x "$PYTHON_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    else
        echo "python 不存在: $PYTHON_BIN" >&2
        exit 1
    fi
fi

mkdir -p "$PROJECT_ROOT/APP/backend/logs"
cd "$PROJECT_ROOT/APP/backend"

export HOME="${HOME:-/Users/$(id -un)}"
export PATH="${CONDA_ENV_PATH}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -d "$CONDA_ENV_PATH" ]; then
    export PYTHONHOME="${PYTHONHOME:-$CONDA_ENV_PATH}"
fi
export PROXY_PORT="$MINI_PROXY_PORT"
export PYTHONUNBUFFERED=1

exec "$PYTHON_BIN" jira_proxy.py --port "$MINI_PROXY_PORT"
