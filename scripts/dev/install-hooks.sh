#!/usr/bin/env bash
# 启用仓库自带的 git 闸门。
#
# 背景：仓库里一直有 .githooks/pre-commit，但从没有人设置 core.hooksPath，
# 所以它【从未生效过】。跑一次本脚本即可为当前 clone 启用。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks

echo "✅ 已启用仓库闸门：core.hooksPath = $(git config core.hooksPath)"
echo "   生效的检查：空白/冲突标记、运行时数据与凭据拦截、schedule 审计、测试伴随告警"
echo "   紧急跳过：git commit --no-verify"
