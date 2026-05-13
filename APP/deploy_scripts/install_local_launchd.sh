#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/network_env.sh"

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

PLIST_DIR="${PLIST_DIR:-$LAUNCHD_HOME/Library/LaunchAgents}"
LOG_DIR="$PROJECT_ROOT/APP/backend/logs"
PLIST_PATH="$PLIST_DIR/com.aiticket.local-backend.plist"
LABEL="com.aiticket.local-backend"
LAUNCHD_DOMAIN="gui/$(id -u)"

detect_ip() {
    local pattern="$1"
    ifconfig 2>/dev/null | awk -v p="$pattern" '$1 == "inet" && $2 ~ p { print $2; exit }'
}

mkdir -p "$PLIST_DIR" "$LOG_DIR"

if ! launchctl print "$LAUNCHD_DOMAIN" >/dev/null 2>&1; then
    LAUNCHD_DOMAIN="user/$(id -u)"
fi

CONDA_ENV_PATH="${CONDA_ENV_PATH:-/Volumes/MacMini/opt/miniconda3/envs/antigravity}"
PYTHON_BIN="${PYTHON_BIN:-$CONDA_ENV_PATH/bin/python3.12}"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${PROJECT_ROOT}/APP/deploy_scripts/run_local_backend.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LOCAL_BACKEND_HOST</key>
        <string>${LOCAL_BACKEND_HOST}</string>
        <key>LOCAL_BACKEND_PORT</key>
        <string>${LOCAL_BACKEND_PORT}</string>
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
        <key>JIRA_SKIP_COOKIES</key>
        <string>${JIRA_SKIP_COOKIES:-true}</string>
        <key>ENABLE_CACHE_SERVICE</key>
        <string>${ENABLE_CACHE_SERVICE:-false}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${LABEL}.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${LABEL}.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "$LAUNCHD_DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true

bootstrap_error=""
if ! bootstrap_error="$(launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_PATH" 2>&1)"; then
    echo "launchd 注册失败:"
    echo "  - domain: ${LAUNCHD_DOMAIN}"
    echo "  - error: ${bootstrap_error}"
    echo "  - plist 已写入: ${PLIST_PATH}"
    echo ""
    echo "说明:"
    echo "  当前这个 shell 会话无法把 LaunchAgent 注册进对应的用户 bootstrap domain。"
    echo "  这通常发生在后台/非 GUI 会话中。"
    echo "  配置文件已经就位，下次 macOS 图形会话登录时会自动尝试加载。"
    exit 1
fi

existing_pid="$(lsof -ti :"$LOCAL_BACKEND_PORT" 2>/dev/null | head -n 1 || true)"
if [ -n "$existing_pid" ]; then
    echo "停止占用端口 ${LOCAL_BACKEND_PORT} 的进程: $existing_pid"
    kill "$existing_pid" >/dev/null 2>&1 || true
    sleep 2
fi

launchctl kickstart -k "${LAUNCHD_DOMAIN}/${LABEL}"

# ── jobmaster daemon（调度器 + KB 写队列，单实例，替代 API in-process 调度）──
JM_DAEMON_LABEL="com.aiticket.local-jobmaster"
JM_DAEMON_PLIST="$PLIST_DIR/${JM_DAEMON_LABEL}.plist"

cat > "$JM_DAEMON_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${JM_DAEMON_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${PROJECT_ROOT}/APP/deploy_scripts/run_local_jobmaster_daemon.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LOCAL_BACKEND_HOST</key>
        <string>${LOCAL_BACKEND_HOST}</string>
        <key>LOCAL_BACKEND_PORT</key>
        <string>${LOCAL_BACKEND_PORT}</string>
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
        <key>RUN_BACKGROUND_JOBS</key>
        <string>1</string>
        <key>JIRA_SKIP_COOKIES</key>
        <string>${JIRA_SKIP_COOKIES:-true}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${JM_DAEMON_LABEL}.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${JM_DAEMON_LABEL}.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "$LAUNCHD_DOMAIN" "$JM_DAEMON_PLIST" >/dev/null 2>&1 || true
if launchctl bootstrap "$LAUNCHD_DOMAIN" "$JM_DAEMON_PLIST" 2>/dev/null; then
    launchctl kickstart -k "${LAUNCHD_DOMAIN}/${JM_DAEMON_LABEL}"
    echo "  - jobmaster daemon 已安装并启动: ${JM_DAEMON_LABEL}"
else
    echo "  - jobmaster daemon plist 已写入（下次登录生效）: ${JM_DAEMON_PLIST}"
fi

TAILSCALE_IP="$(detect_ip '^100\\.')"
LAN_IP="$(detect_ip '^192\\.168\\.')"

echo "已安装并启动 launchd 服务:"
echo "  - ${LABEL}"
echo "  - domain: ${LAUNCHD_DOMAIN}"
echo "  - plist: ${PLIST_PATH}"
echo "  - url: http://127.0.0.1:${LOCAL_BACKEND_PORT}"
if [ -n "$TAILSCALE_IP" ]; then
    echo "  - tailscale (optional): http://${TAILSCALE_IP}:${LOCAL_BACKEND_PORT}"
fi
if [ -n "$LAN_IP" ] && [ "$LAN_IP" != "$TAILSCALE_IP" ]; then
    echo "  - lan: http://${LAN_IP}:${LOCAL_BACKEND_PORT}"
fi
