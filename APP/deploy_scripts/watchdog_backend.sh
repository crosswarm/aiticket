#!/bin/bash
# Watchdog: 每2分钟检查 backend 健康，卡死则 kill（launchd 自动重启）
HEALTH_URL="http://127.0.0.1:3000/api/board/stats"
LOG="/Users/cfone/Library/Logs/aiticket/watchdog.log"
TS=$(date "+%Y-%m-%d %H:%M:%S")

if /Volumes/MacMini/opt/miniconda3/envs/antigravity/bin/python3.12 -c "
import urllib.request, sys
try:
    urllib.request.urlopen('$HEALTH_URL', timeout=8)
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    echo "[$TS] OK" >> "$LOG"
else
    echo "[$TS] HUNG — killing uvicorn workers, launchd will restart" >> "$LOG"
    pkill -9 -f "uvicorn main:app" 2>/dev/null
    pkill -9 -f "agent-browser-chrome" 2>/dev/null
fi
