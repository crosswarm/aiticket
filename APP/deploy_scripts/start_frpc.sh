#!/bin/bash
# frp 客户端启动脚本 (mini)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/frp_common.sh"

mkdir -p "$FRP_DIR"
launcher_log="$FRP_DIR/frpc-launcher.log"

if pgrep -f "frpc.*frpc.ini" >/dev/null 2>&1; then
    warn "发现已有 frpc 进程，执行无提示重启"
    pkill -f "frpc.*frpc.ini" || true
    sleep 2
fi

info "启动 frpc，日志: $launcher_log"
nohup /bin/bash "$SCRIPT_DIR/run_frpc.sh" > "$launcher_log" 2>&1 &
frpc_pid=$!

sleep 3

if ps -p "$frpc_pid" >/dev/null 2>&1; then
    info "frpc 启动成功，PID: $frpc_pid"
    echo "查看日志: tail -f $launcher_log"
    exit 0
fi

error "frpc 启动失败，请检查日志: $launcher_log"
exit 1
