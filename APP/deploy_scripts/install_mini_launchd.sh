#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/network_env.sh"

require_env_msg() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        echo "缺少环境变量: $name" >&2
        exit 1
    fi
}

if [ "$MINI_TUNNEL_MODE" = "frpc" ]; then
    require_env_msg "FRP_TOKEN"

    if [ -z "${FRP_CONNECT_SERVER_LOCAL_IP:-}" ]; then
        FRP_CONNECT_SERVER_LOCAL_IP="$(
            networksetup -getinfo Wi-Fi 2>/dev/null | awk -F': ' '/^IP address: / {print $2; exit}'
        )"
    fi
    if [ -z "${FRP_CONNECT_SERVER_LOCAL_IP:-}" ]; then
        FRP_CONNECT_SERVER_LOCAL_IP="$(
            networksetup -getinfo Ethernet 2>/dev/null | awk -F': ' '/^IP address: / {print $2; exit}'
        )"
    fi
fi

CANONICAL_HOME="/Users/$(id -un)"
DEFAULT_HOME="$CANONICAL_HOME"
if [ ! -d "$DEFAULT_HOME" ]; then
    DEFAULT_HOME="$(python3 - <<'PY'
import os
import pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
)"
fi
LAUNCHD_HOME="${LAUNCHD_HOME:-$DEFAULT_HOME}"
if [ ! -d "$LAUNCHD_HOME" ]; then
    LAUNCHD_HOME="$HOME"
fi

CONDA_ENV_PATH="${CONDA_ENV_PATH:-/Volumes/MacMini/opt/miniconda3/envs/antigravity}"
PYTHON_BIN="${PYTHON_BIN:-$CONDA_ENV_PATH/bin/python3.12}"

PLIST_DIR="${PLIST_DIR:-$LAUNCHD_HOME/Library/LaunchAgents}"
LOG_DIR="$PROJECT_ROOT/APP/backend/logs"
mkdir -p "$PLIST_DIR" "$LOG_DIR"

LAUNCHD_DOMAIN="gui/$(id -u)"
if ! launchctl print "$LAUNCHD_DOMAIN" >/dev/null 2>&1; then
    LAUNCHD_DOMAIN="user/$(id -u)"
fi

write_plist() {
    local target="$1"
    local label="$2"
    local program="$3"
    cat > "$target" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${program}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>FRP_TOKEN</key>
        <string>${FRP_TOKEN:-}</string>
        <key>FRP_SERVER_ADDR</key>
        <string>${FRP_SERVER_ADDR:-}</string>
        <key>FRP_CUSTOM_DOMAIN</key>
        <string>${FRP_CUSTOM_DOMAIN:-localhost}</string>
        <key>FRP_CONNECT_SERVER_LOCAL_IP</key>
        <string>${FRP_CONNECT_SERVER_LOCAL_IP:-}</string>
        <key>QCL_SSH_TARGET</key>
        <string>${QCL_SSH_TARGET}</string>
        <key>REMOTE_TUNNEL_PORT</key>
        <string>${REMOTE_TUNNEL_PORT}</string>
        <key>MINI_TUNNEL_MODE</key>
        <string>${MINI_TUNNEL_MODE}</string>
        <key>MINI_PROXY_PORT</key>
        <string>${MINI_PROXY_PORT}</string>
        <key>FRP_BIND_PORT</key>
        <string>${FRP_BIND_PORT}</string>
        <key>HOME</key>
        <string>${LAUNCHD_HOME}</string>
        <key>PATH</key>
        <string>${CONDA_ENV_PATH}/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>CONDA_ENV_PATH</key>
        <string>${CONDA_ENV_PATH}</string>
        <key>PYTHONHOME</key>
        <string>${CONDA_ENV_PATH}</string>
        <key>PYTHON_BIN</key>
        <string>${PYTHON_BIN}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${label}.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${label}.err.log</string>
</dict>
</plist>
EOF
}

jira_plist="$PLIST_DIR/com.aiticket.jira-proxy.plist"
tunnel_plist="$PLIST_DIR/com.aiticket.frpc.plist"
tunnel_label="com.aiticket.frpc"
tunnel_program="$PROJECT_ROOT/APP/deploy_scripts/run_frpc.sh"

if [ "$MINI_TUNNEL_MODE" = "ssh_reverse" ]; then
    tunnel_plist="$PLIST_DIR/com.aiticket.qcl-tunnel.plist"
    tunnel_label="com.aiticket.qcl-tunnel"
    tunnel_program="$PROJECT_ROOT/APP/deploy_scripts/run_qcl_reverse_tunnel.sh"
fi

write_plist "$jira_plist" "com.aiticket.jira-proxy" "$PROJECT_ROOT/APP/deploy_scripts/run_jira_proxy.sh"
write_plist "$tunnel_plist" "$tunnel_label" "$tunnel_program"

launchctl bootout "$LAUNCHD_DOMAIN" "$jira_plist" >/dev/null 2>&1 || true
launchctl bootout "$LAUNCHD_DOMAIN" "$PLIST_DIR/com.aiticket.frpc.plist" >/dev/null 2>&1 || true
launchctl bootout "$LAUNCHD_DOMAIN" "$PLIST_DIR/com.aiticket.qcl-tunnel.plist" >/dev/null 2>&1 || true

bootstrap_error=""
if ! bootstrap_error="$(launchctl bootstrap "$LAUNCHD_DOMAIN" "$jira_plist" 2>&1)"; then
    echo "launchd 注册 jira-proxy 失败:"
    echo "  - domain: ${LAUNCHD_DOMAIN}"
    echo "  - error: ${bootstrap_error}"
    echo "  - plist 已写入: ${jira_plist}"
    exit 1
fi

if ! bootstrap_error="$(launchctl bootstrap "$LAUNCHD_DOMAIN" "$tunnel_plist" 2>&1)"; then
    echo "launchd 注册 tunnel 失败:"
    echo "  - domain: ${LAUNCHD_DOMAIN}"
    echo "  - error: ${bootstrap_error}"
    echo "  - plist 已写入: ${tunnel_plist}"
    exit 1
fi

launchctl kickstart -k "${LAUNCHD_DOMAIN}/com.aiticket.jira-proxy"
launchctl kickstart -k "${LAUNCHD_DOMAIN}/${tunnel_label}"

echo "已安装并启动 launchd 服务:"
echo "  - com.aiticket.jira-proxy"
echo "  - ${tunnel_label}"
