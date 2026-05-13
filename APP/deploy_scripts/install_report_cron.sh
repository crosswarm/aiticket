#!/bin/bash
# 安装报告数据自动采集定时任务 (launchd)
# 周报: 每周日 00:00 采集
# 月报: 每月28日 00:00 采集

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CANONICAL_HOME="/Users/$(id -un)"
DEFAULT_HOME="$CANONICAL_HOME"
if [ ! -d "$DEFAULT_HOME" ]; then
    DEFAULT_HOME="$(python3 -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
fi
LAUNCHD_HOME="${LAUNCHD_HOME:-$DEFAULT_HOME}"
if [ ! -d "$LAUNCHD_HOME" ]; then
    LAUNCHD_HOME="$HOME"
fi

PLIST_DIR="${PLIST_DIR:-$LAUNCHD_HOME/Library/LaunchAgents}"
LOG_DIR="$PROJECT_ROOT/APP/backend/logs"
LAUNCHD_DOMAIN="gui/$(id -u)"

CONDA_ENV_PATH="${CONDA_ENV_PATH:-/Volumes/MacMini/opt/miniconda3/envs/antigravity}"
PYTHON_BIN="${PYTHON_BIN:-$CONDA_ENV_PATH/bin/python3.12}"
AUTO_REPORT_SCRIPT="$PROJECT_ROOT/APP/backend/scripts/auto_report_data.py"

mkdir -p "$PLIST_DIR" "$LOG_DIR"

if ! launchctl print "$LAUNCHD_DOMAIN" >/dev/null 2>&1; then
    LAUNCHD_DOMAIN="user/$(id -u)"
fi

install_plist() {
    local label="$1"
    local plist_path="$PLIST_DIR/${label}.plist"
    local args="$2"
    local calendar_xml="$3"

    cat > "$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${AUTO_REPORT_SCRIPT}</string>
        ${args}
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>StartCalendarInterval</key>
    ${calendar_xml}
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${LAUNCHD_HOME}</string>
        <key>PATH</key>
        <string>${CONDA_ENV_PATH}/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>CONDA_ENV_PATH</key>
        <string>${CONDA_ENV_PATH}</string>
        <key>PYTHONHOME</key>
        <string>${CONDA_ENV_PATH}</string>
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

    launchctl bootout "$LAUNCHD_DOMAIN" "$plist_path" >/dev/null 2>&1 || true

    if launchctl bootstrap "$LAUNCHD_DOMAIN" "$plist_path" 2>/dev/null; then
        echo "  ✅ ${label} 已注册"
    else
        echo "  ⚠️  ${label} 注册失败 (plist已写入, 下次登录自动加载)"
    fi
}

echo "安装报告数据自动采集定时任务..."

# 周报: 每周日 00:00
install_plist "com.aiticket.weekly-data-collect" \
    "<string>--weekly</string>" \
    "<dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>0</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>"

# 月报: 每月28-31日 00:00 触发，脚本内判断是否为当月最后一天
install_plist "com.aiticket.monthly-data-collect" \
    "<string>--monthly</string>
        <string>--last-day-only</string>" \
    "<array>
        <dict>
            <key>Day</key>
            <integer>28</integer>
            <key>Hour</key>
            <integer>0</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
        <dict>
            <key>Day</key>
            <integer>29</integer>
            <key>Hour</key>
            <integer>0</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
        <dict>
            <key>Day</key>
            <integer>30</integer>
            <key>Hour</key>
            <integer>0</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
        <dict>
            <key>Day</key>
            <integer>31</integer>
            <key>Hour</key>
            <integer>0</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
    </array>"

echo ""
echo "定时任务已安装:"
echo "  - 周报采集: 每周日 00:00 (com.aiticket.weekly-data-collect)"
echo "  - 月报采集: 每月最后一天 00:00 (com.aiticket.monthly-data-collect, 28-31日触发+脚本判断)"
echo "  - 日志: ${LOG_DIR}/"
echo ""
echo "验证: launchctl list | grep aiticket"
