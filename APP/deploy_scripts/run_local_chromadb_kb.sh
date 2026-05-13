#!/bin/bash
# Chroma server for KB (data/chroma_kb, port 8001)
# 仅在 CHROMA_MODE=server 时需要此 daemon；默认 persistent 模式不启动。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CONDA_ENV_PATH="${CONDA_ENV_PATH:-/Volumes/MacMini/opt/miniconda3/envs/antigravity}"
CHROMA_BIN="${CONDA_ENV_PATH}/bin/chroma"

if [ ! -x "$CHROMA_BIN" ]; then
    echo "chroma 不存在: $CHROMA_BIN" >&2
    exit 1
fi

CHROMA_PATH="$PROJECT_ROOT/data/chroma_kb"
mkdir -p "$CHROMA_PATH"

unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy

exec "$CHROMA_BIN" run \
    --path "$CHROMA_PATH" \
    --host 127.0.0.1 \
    --port 8001
