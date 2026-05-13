#!/bin/bash
# mini 本地环境零停机升级脚本
# 用法: ./local_upgrade.sh [port]

set -e

# 配置
PROJECT_DIR="/Volumes/MacMini/Users/cfone/Documents/用友/AI工单"
BACKEND_DIR="$PROJECT_DIR/APP/backend"
LOGS_DIR="$BACKEND_DIR/logs"
PROXY_PORT="${1:-5001}"  # 默认使用 5001 端口 (避免与 AirPlay 冲突)
MAIN_PORT=8000

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查端口是否可用
check_port() {
    local port=$1
    if lsof -i :$port > /dev/null 2>&1; then
        return 1
    fi
    return 0
}

# 检查服务健康状态
check_health() {
    local url=$1
    local max_attempts=${2:-5}
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if curl -s "$url" > /dev/null 2>&1; then
            return 0
        fi
        log_info "等待服务启动... ($attempt/$max_attempts)"
        sleep 2
        attempt=$((attempt + 1))
    done
    return 1
}

# 主流程
main() {
    log_info "开始 mini 本地环境零停机升级..."
    log_info "项目目录: $PROJECT_DIR"
    log_info "代理服务端口: $PROXY_PORT"

    # 1. 备份当前状态
    log_info "备份配置和日志..."
    cd "$PROJECT_DIR"

    if [ -f "APP/backend/config/config.json" ]; then
        cp APP/backend/config/config.json "APP/backend/config/config.json.backup.$(date +%Y%m%d_%H%M%S)"
        log_info "已备份 config.json"
    fi

    if [ -f "APP/backend/data/board_config.json" ]; then
        cp APP/backend/data/board_config.json "APP/backend/data/board_config.json.backup.$(date +%Y%m%d_%H%M%S)"
        log_info "已备份 board_config.json"
    fi

    # 2. 创建 Git 标签
    log_info "创建 Git 标签用于回退..."
    git add . > /dev/null 2>&1 || true
    git commit -m "pre-upgrade: 备份升级前状态" > /dev/null 2>&1 || true
    git tag "v$(date +%Y.%m.%d)-pre-upgrade" 2>/dev/null || true
    log_info "Git 标签已创建: v$(date +%Y.%m.%d)-pre-upgrade"

    # 3. 验证端口可用性
    log_info "检查端口 $PROXY_PORT 可用性..."
    if ! check_port $PROXY_PORT; then
        log_warn "端口 $PROXY_PORT 已被占用"
        # 检查是否已经是 jira_proxy
        if lsof -i :$PROXY_PORT | grep -q "jira_proxy"; then
            log_info "端口 $PROXY_PORT 已被 jira_proxy 占用，跳过启动"
        else
            log_error "端口 $PROXY_PORT 被其他进程占用，请选择其他端口"
            exit 1
        fi
    else
        log_info "端口 $PROXY_PORT 可用"
    fi

    # 4. 确认主服务运行
    log_info "检查主服务 (端口 $MAIN_PORT) 状态..."
    if ! check_port $MAIN_PORT; then
        log_info "主服务正在端口 $MAIN_PORT 运行"
    else
        log_warn "主服务未在端口 $MAIN_PORT 运行"
    fi

    # 5. 启动 jira_proxy 服务
    log_info "启动 jira_proxy 服务..."
    cd "$BACKEND_DIR"

    # 创建日志目录
    mkdir -p "$LOGS_DIR"

    # 检查是否已经在运行
    if pgrep -f "jira_proxy.py.*--port $PROXY_PORT" > /dev/null; then
        log_info "jira_proxy (端口 $PROXY_PORT) 已在运行"
    else
        MINI_PROXY_PORT="$PROXY_PORT" nohup bash ../deploy_scripts/run_jira_proxy.sh > "$LOGS_DIR/jira_proxy_$PROXY_PORT.log" 2>&1 &
        PROXY_PID=$!
        log_info "jira_proxy 启动 PID: $PROXY_PID"

        # 等待服务启动
        sleep 3

        # 验证启动成功
        if check_health "http://localhost:$PROXY_PORT/proxy/health" 5; then
            log_info "jira_proxy 启动成功"
        else
            log_error "jira_proxy 启动失败，查看日志: $LOGS_DIR/jira_proxy_$PROXY_PORT.log"
            exit 1
        fi
    fi

    # 6. 验证服务
    log_info "验证 jira_proxy 服务..."
    HEALTH=$(curl -s "http://localhost:$PROXY_PORT/proxy/health")
    if echo "$HEALTH" | grep -q '"status":"success"'; then
        log_info "✅ jira_proxy 健康检查通过"
    else
        log_error "❌ jira_proxy 健康检查失败"
        exit 1
    fi

    # 测试字段接口
    if curl -s "http://localhost:$PROXY_PORT/proxy/jira/fields" > /dev/null 2>&1; then
        log_info "✅ 字段接口测试通过"
    else
        log_warn "⚠️ 字段接口测试失败 (可能 VPN 未连接)"
    fi

    # 7. 验证现有服务不受影响
    log_info "验证现有主服务..."
    if curl -s "http://localhost:$MAIN_PORT/api/board" > /dev/null 2>&1; then
        log_info "✅ 主服务看板接口正常"
    else
        log_warn "⚠️ 主服务看板接口异常"
    fi

    # 8. 完成
    log_info "========================================"
    log_info "🎉 升级完成！"
    log_info "========================================"
    log_info "服务状态:"
    log_info "  - 主服务 (main.py): http://localhost:$MAIN_PORT"
    log_info "  - 代理服务 (jira_proxy): http://localhost:$PROXY_PORT"
    log_info ""
    log_info "常用命令:"
    log_info "  - 查看代理日志: tail -f $LOGS_DIR/jira_proxy_$PROXY_PORT.log"
    log_info "  - 停止代理服务: pkill -f 'jira_proxy.py.*--port $PROXY_PORT'"
    log_info "  - 健康检查: curl http://localhost:$PROXY_PORT/proxy/health"
    log_info ""
    log_info "如需回退，运行: ./rollback_local.sh"
}

# 执行主流程
main "$@"
