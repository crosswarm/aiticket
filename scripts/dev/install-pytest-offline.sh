#!/usr/bin/env bash
# 给【离线】服务器的容器装 pytest，不改镜像、不改 requirements.txt。
#
# 原理：pytest 及其依赖（pluggy / iniconfig / packaging）都是纯 Python，
# 装进容器的 /data（命名卷，容器 recreate 不丢）即可，运行时完全不受影响。
#
# 用法：
#   1) 有网的机器上抓 wheel：
#        bash scripts/dev/install-pytest-offline.sh fetch [输出目录]
#      默认输出到 _local/wheels（已 gitignore，不入库）
#   2) 把该目录拷到目标机后，在目标机上装进容器：
#        bash scripts/dev/install-pytest-offline.sh install <wheels目录> [容器名]
#   3) 目标容器本身有网时可一步到位：
#        bash scripts/dev/install-pytest-offline.sh direct [容器名]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${1:-}"
CONTAINER_DEFAULT="aiticket"
TARGET_DIR="/data/pytools"

case "$MODE" in
  fetch)
    OUT="${2:-$REPO_ROOT/_local/wheels}"
    mkdir -p "$OUT"
    # 与容器一致的 Python 3.11 / manylinux 平台，避免抓到 mac 平台的包
    python3 -m pip download pytest \
      --dest "$OUT" \
      --only-binary=:all: \
      --python-version 3.11 --implementation cp \
      --platform manylinux2014_x86_64
    echo "✅ wheel 已下载到 $OUT"
    ls -1 "$OUT" | sed 's/^/   /'
    echo "   下一步：把该目录拷到目标机，再执行 install 模式"
    ;;

  install)
    WHEELS="${2:?用法: install <wheels目录> [容器名]}"
    CONTAINER="${3:-$CONTAINER_DEFAULT}"
    [ -d "$WHEELS" ] || { echo "wheels 目录不存在: $WHEELS" >&2; exit 2; }
    docker exec "$CONTAINER" mkdir -p /data/wheels
    docker cp "$WHEELS/." "$CONTAINER:/data/wheels/"
    docker exec "$CONTAINER" pip install --quiet --no-index \
      --find-links=/data/wheels --target="$TARGET_DIR" pytest
    docker exec "$CONTAINER" sh -lc "PYTHONPATH=$TARGET_DIR python3 -c 'import pytest;print(\"pytest\", pytest.__version__)'"
    echo "✅ 已装入 $CONTAINER:$TARGET_DIR（持久卷，容器重建不丢）"
    ;;

  direct)
    CONTAINER="${2:-$CONTAINER_DEFAULT}"
    docker exec "$CONTAINER" pip install --quiet --no-warn-script-location \
      --target="$TARGET_DIR" pytest
    docker exec "$CONTAINER" sh -lc "PYTHONPATH=$TARGET_DIR python3 -c 'import pytest;print(\"pytest\", pytest.__version__)'"
    echo "✅ 已装入 $CONTAINER:$TARGET_DIR（持久卷，容器重建不丢）"
    ;;

  *)
    sed -n '2,20p' "${BASH_SOURCE[0]}"
    exit 2
    ;;
esac
