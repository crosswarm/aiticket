#!/bin/bash
# sync_data.sh - 同步向量数据库和其他数据文件到服务器
# 用法: ./sync_data.sh [--full] [--pull]
#   --pull  反向同步：QCL → 本机（用于恢复 runtime 状态文件）
#   --full  同步 conclusion/ 目录（仅 push 方向有效）

set -e

# 配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SERVER="qcl"
QCL_BACKEND_PORT="${QCL_BACKEND_PORT:-18000}"
REMOTE_DIR="/opt/ai-ticket"

# 参数解析
MODE="push"
FULL=false
for arg in "$@"; do
    case "$arg" in
        --pull) MODE="pull" ;;
        --full) FULL=true ;;
    esac
done

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}📦 数据同步工具${NC}"
echo -e "${GREEN}========================================${NC}"

# 检查服务器连接
echo -e "${YELLOW}[1/4] 检查服务器连接...${NC}"
if ! ssh -o ConnectTimeout=5 $SERVER "echo '连接成功'" > /dev/null 2>&1; then
    echo -e "${RED}❌ 无法连接到服务器${NC}"
    exit 1
fi
echo -e "${GREEN}✓ 服务器连接正常${NC}"

# 停止服务（避免数据冲突）
echo -e "${YELLOW}[2/4] 停止后端服务...${NC}"
if [ "$MODE" = "pull" ]; then
    echo -e "${GREEN}✓ pull 模式：不停 QCL 服务${NC}"
else
    ssh $SERVER "sudo supervisorctl stop ai-ticket" 2>/dev/null || true
    echo -e "${GREEN}✓ QCL 服务已停止${NC}"
fi

# 同步数据
echo -e "${YELLOW}[3/4] 同步数据文件...${NC}"

# 同步chroma_db（board向量库）
echo "  同步 chroma/ticket（board向量库）..."
rsync -avz --delete "${LOCAL_DIR}/APP/data/chroma/ticket/" "${SERVER}:${REMOTE_DIR}/APP/data/chroma/ticket/"

# 同步chroma_kb（KB向量库）
echo "  同步 APP/data/chroma/kb（KB向量库）..."
ssh "${SERVER}" "mkdir -p ${REMOTE_DIR}/APP/data/chroma/kb"
rsync -avz --delete "${LOCAL_DIR}/APP/data/chroma/kb/" "${SERVER}:${REMOTE_DIR}/APP/data/chroma/kb/"

# 同步KB编译索引（sqlite）
echo "  同步 sqlite KB索引..."
ssh "${SERVER}" "mkdir -p ${REMOTE_DIR}/APP/backend/data/sqlite"
rsync -avz "${LOCAL_DIR}/APP/backend/data/sqlite/kb_chunks.db" "${SERVER}:${REMOTE_DIR}/APP/backend/data/sqlite/" 2>/dev/null || true
rsync -avz "${LOCAL_DIR}/APP/backend/data/sqlite/kb_jobs.db"   "${SERVER}:${REMOTE_DIR}/APP/backend/data/sqlite/" 2>/dev/null || true

# 同步JobMaster/需求池状态（agent_tasks + jobmaster db）
echo "  同步 jobmaster + agent_tasks..."
rsync -avz "${LOCAL_DIR}/APP/backend/data/sqlite/agent_tasks.db" "${SERVER}:${REMOTE_DIR}/APP/backend/data/sqlite/" 2>/dev/null || true
rsync -avz "${LOCAL_DIR}/APP/backend/data/jobmaster.db"          "${SERVER}:${REMOTE_DIR}/APP/backend/data/" 2>/dev/null || true

# 同步conclusion数据（如果指定--full，仅 push 方向）
if $FULL && [ "$MODE" = "push" ]; then
    echo "  同步 conclusion..."
    rsync -avz --exclude='*.pyc' --exclude='__pycache__' "${LOCAL_DIR}/conclusion/" "${SERVER}:${REMOTE_DIR}/conclusion/"
fi

# 同步LLM配置
echo "  同步 llm_config.json..."
rsync -avz "${LOCAL_DIR}/APP/backend/llm_config.json" "${SERVER}:${REMOTE_DIR}/APP/backend/"

# 同步 KB/INDEX（manifest + compiled FILE 内容，不走 git）
echo "  同步 KB/INDEX..."
ssh "${SERVER}" "mkdir -p ${REMOTE_DIR}/KB/INDEX/FILES"
rsync -avz "${LOCAL_DIR}/KB/INDEX/manifest.json" "${SERVER}:${REMOTE_DIR}/KB/INDEX/" 2>/dev/null || true
rsync -avz --delete "${LOCAL_DIR}/KB/INDEX/FILES/" "${SERVER}:${REMOTE_DIR}/KB/INDEX/FILES/" 2>/dev/null || true

# 同步 runtime AI 状态文件（学习产物，不走 git）
RUNTIME_FILES=(board_config.json gate_decisions.jsonl learned_patterns.json \
               move_history.json product_facts.md reply_style_rules.md \
               pending_batch_approve.jsonl)
if [ "$MODE" = "pull" ]; then
    echo "  拉取 runtime data files (QCL → 本机)..."
    ssh "${SERVER}" "mkdir -p ${REMOTE_DIR}/APP/backend/data" 2>/dev/null || true
    for f in "${RUNTIME_FILES[@]}"; do
        rsync -avz "${SERVER}:${REMOTE_DIR}/APP/backend/data/${f}" \
              "${LOCAL_DIR}/APP/backend/data/" 2>/dev/null || true
    done
    if ssh "${SERVER}" "[ -d ${REMOTE_DIR}/APP/backend/data/runtime ]" 2>/dev/null; then
        mkdir -p "${LOCAL_DIR}/APP/backend/data/runtime"
        rsync -avz "${SERVER}:${REMOTE_DIR}/APP/backend/data/runtime/" \
              "${LOCAL_DIR}/APP/backend/data/runtime/" 2>/dev/null || true
    fi
else
    echo "  同步 runtime data files (本机 → QCL)..."
    for f in "${RUNTIME_FILES[@]}"; do
        rsync -avz "${LOCAL_DIR}/APP/backend/data/${f}" \
              "${SERVER}:${REMOTE_DIR}/APP/backend/data/" 2>/dev/null || true
    done
    if [ -d "${LOCAL_DIR}/APP/backend/data/runtime" ]; then
        ssh "${SERVER}" "mkdir -p ${REMOTE_DIR}/APP/backend/data/runtime"
        rsync -avz "${LOCAL_DIR}/APP/backend/data/runtime/" \
              "${SERVER}:${REMOTE_DIR}/APP/backend/data/runtime/" 2>/dev/null || true
    fi
fi

# 同步 daily reports（历史日报，不走 git，仅 push 方向）
if [ "$MODE" != "pull" ]; then
    echo "  同步 conclusion/daily_reports..."
    ssh "${SERVER}" "mkdir -p ${REMOTE_DIR}/APP/backend/conclusion/daily_reports"
    rsync -avz "${LOCAL_DIR}/APP/backend/conclusion/daily_reports/" \
          "${SERVER}:${REMOTE_DIR}/APP/backend/conclusion/daily_reports/" 2>/dev/null || true
fi

echo -e "${GREEN}✓ 数据同步完成${NC}"

# 重启服务
echo -e "${YELLOW}[4/4] 重启后端服务...${NC}"
if [ "$MODE" = "pull" ]; then
    echo -e "${GREEN}✓ pull 模式：本机 board_config 已就绪，无需操作 QCL 服务${NC}"
else
    ssh $SERVER "sudo supervisorctl start ai-ticket"
    sleep 5
fi

# 验证
echo -e "${YELLOW}验证服务状态...${NC}"
if [ "$MODE" = "pull" ]; then
    STATUS=$(curl --noproxy "*" -s -o /dev/null -w '%{http_code}' http://localhost:3000/api/board/stats 2>/dev/null || echo "000")
    if [ "$STATUS" = "200" ]; then
        COLS=$(curl --noproxy "*" -s http://localhost:3000/api/config/board 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('columns',[])),'列,',len(d.get('automation_rules',[])),'条规则')" 2>/dev/null || echo "?")
        echo -e "${GREEN}✅ 本机服务正常，board_config: ${COLS}${NC}"
    else
        echo -e "${RED}⚠️ 本机服务状态异常 (HTTP ${STATUS})${NC}"
    fi
else
    STATUS=$(ssh $SERVER "curl -s -o /dev/null -w '%{http_code}' http://localhost:${QCL_BACKEND_PORT}/api/board/stats")
    if [ "$STATUS" = "200" ]; then
        echo -e "${GREEN}✅ QCL 服务运行正常${NC}"
    else
        echo -e "${RED}⚠️ QCL 服务状态异常 (HTTP ${STATUS})${NC}"
    fi
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ 数据同步完成！${NC}"
echo -e "${GREEN}========================================${NC}"
