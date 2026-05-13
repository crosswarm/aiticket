#!/bin/bash
# Jira 会话保鲜看门狗：每 30 分钟从本机 Chrome 解密 cookies，写入 /tmp/jira-session.json
# 部署：仅在 lap 本机通过 launchd 注册（com.aiticket.jira-session-watchdog.plist）
# StartInterval 1800

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REFRESH_SCRIPT="$SCRIPT_DIR/../backend/scripts/refresh_jira_session.sh"
LOG="/Users/cfone/Library/Logs/aiticket/jira-session-watchdog.log"
TS=$(date "+%Y-%m-%d %H:%M:%S")
mkdir -p "$(dirname "$LOG")"

if [ ! -f "$REFRESH_SCRIPT" ]; then
    echo "[$TS] ERROR: refresh_jira_session.sh not found at $REFRESH_SCRIPT" >> "$LOG"
    exit 1
fi

if bash "$REFRESH_SCRIPT" >/dev/null 2>&1; then
    echo "[$TS] refreshed OK" >> "$LOG"
else
    echo "[$TS] refresh FAILED (exit $?)" >> "$LOG"
fi
