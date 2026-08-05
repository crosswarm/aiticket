#!/usr/bin/env bash
# 统一测试入口：本机 / 容器 / 172 一个命令。
#
# 为什么需要它：这套代码对运行环境很挑，踩过两次坑——
#   1) 172 离线且镜像 requirements 不含测试依赖 → 直接 pytest 是 "No module named pytest"；
#   2) Mac 上唯一自带 pytest 的是 /usr/bin/python3 (3.9)，而代码用了 PEP 604 的
#      `X | None` 语法，需要 3.10+ → 那个 python 连 app 代码都 import 不了。
# 所以这里按「版本够 + 有 pytest」两个条件去探测，而不是写死某个解释器。
#
# 用法:
#   bash scripts/dev/run-tests.sh                     # 跑全部
#   bash scripts/dev/run-tests.sh tests/test_x.py     # 跑指定文件
#   AITICKET_PYTHON=/path/to/python3 bash scripts/dev/run-tests.sh   # 显式指定解释器
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/APP/backend"
# 装在持久卷里的 pytest（容器/172 用；容器 recreate 不丢）
PYTOOLS="${AITICKET_PYTOOLS:-/data/pytools}"

cd "$BACKEND_DIR" || { echo "找不到 $BACKEND_DIR" >&2; exit 2; }

py_ok() {  # $1=解释器路径；要求 >= 3.10（PEP 604 语法）
  [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
}

has_pytest() {  # $1=解释器，$2=可选 PYTHONPATH
  if [ -n "${2:-}" ]; then
    PYTHONPATH="$2" "$1" -c 'import pytest' 2>/dev/null
  else
    "$1" -c 'import pytest' 2>/dev/null
  fi
}

CANDIDATES=(
  "${AITICKET_PYTHON:-}"
  "/Volumes/MacMini/opt/miniconda3/envs/antigravity/bin/python3.12"
  "python3.12" "python3.11" "python3.10" "python3"
)

PY=""; EXTRA_PYTHONPATH=""
for c in "${CANDIDATES[@]}"; do
  [ -n "$c" ] || continue
  py_ok "$c" || continue
  # 优先用持久卷里的 pytest（容器/172 场景）
  if [ -d "$PYTOOLS" ] && has_pytest "$c" "$PYTOOLS"; then
    PY="$c"; EXTRA_PYTHONPATH="$PYTOOLS"; break
  fi
  if has_pytest "$c"; then PY="$c"; break; fi
done

if [ -n "$PY" ]; then
  echo "▶ pytest  解释器: $PY$([ -n "$EXTRA_PYTHONPATH" ] && echo "  (PYTHONPATH=$EXTRA_PYTHONPATH)")"
  if [ -n "$EXTRA_PYTHONPATH" ]; then
    PYTHONPATH="$EXTRA_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m pytest "$@"
  else
    "$PY" -m pytest "$@"
  fi
  exit $?
fi

# ── 宿主没有可用 Python 时，委派进容器跑 ──────────────────────────────────
# 172 服务器就是这种情况：宿主连 python3 都没有，但 <repo>/APP 是 bind-mount
# 进容器的 /app/APP，所以容器里跑到的就是当前工作区的测试文件。
# （注意 scripts/ 不在挂载范围内，所以这里直接调 pytest，不能调容器内的本脚本。）
CONTAINER="${AITICKET_CONTAINER:-aiticket}"
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  echo "▶ 宿主无可用 Python，委派到容器 $CONTAINER 内执行"
  docker exec -e PYTHONPATH="$PYTOOLS" -w /app/APP/backend "$CONTAINER" \
    python3 -m pytest "$@"
  exit $?
fi

# ── 兜底：谁都没有 pytest 时，用标准库 unittest 至少跑起来 ──
# 对应 ycc-approve-inbox「零依赖测试」的精神：任何环境都必须有办法验证，
# 哪怕只能覆盖不依赖 pytest fixture 的那部分测试。
FALLBACK=""
for c in "${CANDIDATES[@]}"; do
  [ -n "$c" ] || continue
  py_ok "$c" && { FALLBACK="$c"; break; }
done
[ -n "$FALLBACK" ] || { echo "✖ 找不到 >=3.10 的 Python，无法运行测试" >&2; exit 2; }

echo "⚠ 未找到 pytest，降级用标准库 unittest（仅覆盖不依赖 pytest fixture 的测试）"
echo "  要跑全量请先装 pytest：见 scripts/dev/install-pytest-offline.sh"
echo "▶ unittest  解释器: $FALLBACK"
"$FALLBACK" -m unittest discover -s tests -p 'test_*.py' -v
