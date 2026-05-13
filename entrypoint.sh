#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
CONFIG_FILE="${CONFIG_FILE:-/app/config/deployment.yaml}"
PORT="${PORT:-18000}"

# Validate required config
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: deployment.yaml not found at $CONFIG_FILE"
    echo "  Mount it via: -v ./APP/backend/config/deployment.yaml:/app/config/deployment.yaml:ro"
    exit 1
fi

# Bootstrap DB schema on first run (idempotent)
echo "[entrypoint] 初始化数据库 schema..."
python -m bootstrap.init_db --data-dir "$DATA_DIR"

# Start uvicorn
echo "[entrypoint] 启动 aiticket (port $PORT)..."
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 1 \
    --log-level info \
    --access-log
