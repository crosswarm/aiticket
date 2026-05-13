#!/usr/bin/env bash
# 运行全部压测场景 C1-C6，输出 JSON 结果。
# 用法: bash run_all.sh [BASE_URL] [ADMIN_USER] [ADMIN_PASS]
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${1:-http://127.0.0.1:18000}"
ADMIN_USER="${2:-admin}"
ADMIN_PASS="${3:-admin}"
DATE=$(date +%Y%m%d_%H%M%S)
OUT="$DIR/results-${DATE}.json"

echo "=== aiticket 压测 ==="
echo "目标: $BASE_URL"
echo "结果: $OUT"
echo ""

# 前置检查：服务健康
if ! curl -fsS "$BASE_URL/api/instance/config" > /dev/null 2>&1; then
    echo "❌ 服务未响应 $BASE_URL/api/instance/config — 请先启动后端"
    exit 1
fi
echo "✅ 服务健康检查通过"
echo ""

# 执行各场景
python3 "$DIR/run_tests.py" \
    --base-url "$BASE_URL" \
    --admin-user "$ADMIN_USER" \
    --admin-pass "$ADMIN_PASS" \
    --output "$OUT"

echo ""
echo "✅ 压测完成，结果: $OUT"
echo ""
python3 "$DIR/build_report.py" "$OUT"
