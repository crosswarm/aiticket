#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
# 容器内固定监听 18000（对齐 EXPOSE / healthcheck / compose 容器侧）。
# 宿主发布端口由 compose 的 ${PORT:-18000}:18000 控制，PORT 只改宿主侧，不改容器内。
# 旧版容器内用 --port $PORT 而 compose 容器侧写死 18000，一旦 .env 改 PORT
# 就映射到无人监听的端口 → healthcheck 永远 unhealthy。
INTERNAL_PORT=18000

# 必须切到 backend 目录再启动：代码 hardwired 依赖 CWD/相对布局
# （bootstrap 包、config/loader 的 config/deployment.yaml、__file__ 相对路径）。
cd /app/APP/backend
export PYTHONPATH=/app/APP/backend:${PYTHONPATH:-}

# 配置：config/loader.py 自查 CONFIG_FILE 环境变量 / /app/config/deployment.yaml /
# __file__.parent/deployment.yaml。镜像已内置默认 config/deployment.yaml，
# 未挂载 host 配置也能起；找不到则 loader 用内置默认值，不硬退出。
if [ -n "${CONFIG_FILE:-}" ] && [ ! -f "$CONFIG_FILE" ]; then
    echo "[entrypoint] 警告：CONFIG_FILE=$CONFIG_FILE 不存在，改用镜像内置默认配置"
fi

# Bootstrap DB schema on first run (idempotent)
echo "[entrypoint] 初始化数据库 schema..."
python -m bootstrap.init_db --data-dir "$DATA_DIR"

# Start uvicorn
echo "[entrypoint] 启动 aiticket (container port $INTERNAL_PORT)..."
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "$INTERNAL_PORT" \
    --workers 1 \
    --limit-concurrency 64 \
    --timeout-keep-alive 5 \
    --log-level info \
    --access-log
