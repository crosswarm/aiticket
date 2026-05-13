#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/frp_common.sh"

ensure_frp_binary "frpc"
render_frpc_config

proxy_health_url="http://127.0.0.1:${MINI_PROXY_PORT}/proxy/health"
wait_seconds="${JIRA_PROXY_STARTUP_WAIT_SEC:-30}"
deadline=$((SECONDS + wait_seconds))

until curl -fsS --max-time 2 "$proxy_health_url" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        error "等待 jira_proxy 健康检查超时: $proxy_health_url"
        exit 1
    fi
    warn "等待 jira_proxy 就绪: $proxy_health_url"
    sleep 2
done

cd "$FRP_DIR"
exec ./frpc -c frpc.ini
