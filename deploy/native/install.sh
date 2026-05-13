#!/usr/bin/env bash
# aiticket 原生安装脚本（Ubuntu 22.04+）
set -euo pipefail

INSTALL_DIR="/opt/aiticket"
DATA_DIR="/data/aiticket"
CONFIG_DIR="/etc/aiticket"
LOG_DIR="/var/log/aiticket"
SERVICE_USER="www-data"

check_python() {
    if ! python3 --version 2>&1 | grep -qE '3\.(11|12|13)'; then
        echo "ERROR: 需要 Python 3.11+，当前：$(python3 --version 2>&1)"
        echo "  Ubuntu: sudo apt install python3.11 python3.11-venv"
        exit 1
    fi
}

install_system_deps() {
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip \
        curl sqlite3 build-essential git nginx
}

setup_dirs() {
    mkdir -p "$INSTALL_DIR" "$DATA_DIR/sqlite" "$DATA_DIR/chroma" \
             "$DATA_DIR/kb" "$DATA_DIR/reply_trainer" \
             "$CONFIG_DIR" "$LOG_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"
}

install_app() {
    # Copy application files
    cp -r APP/backend/. "$INSTALL_DIR/backend/"
    cp -r APP/frontend/. "$INSTALL_DIR/frontend/"

    # Create virtualenv and install deps
    python3.11 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --no-cache-dir -r "$INSTALL_DIR/backend/requirements.txt"
}

install_service() {
    cp deploy/native/aiticket.service /etc/systemd/system/aiticket.service
    systemctl daemon-reload
    systemctl enable aiticket
}

main() {
    echo "=== aiticket 安装向导 ==="
    [ "$(id -u)" -ne 0 ] && { echo "请以 root 运行"; exit 1; }

    check_python
    read -p "安装系统依赖？[y/N]: " yn
    [[ "$yn" =~ ^[Yy]$ ]] && install_system_deps

    setup_dirs
    install_app
    install_service

    echo ""
    echo "✅ 安装完成！后续步骤："
    echo "  1. 复制配置：cp APP/backend/config/deployment.yaml.example $CONFIG_DIR/deployment.yaml"
    echo "  2. 编辑配置：nano $CONFIG_DIR/deployment.yaml"
    echo "  3. 复制 env：cp .env.example $CONFIG_DIR/.env && nano $CONFIG_DIR/.env"
    echo "  4. 创建 admin：$INSTALL_DIR/venv/bin/python -m bootstrap.seed_admin"
    echo "  5. 启动服务：systemctl start aiticket"
    echo "  6. 查看日志：journalctl -u aiticket -f"
    echo ""
    echo "  详细说明见 BOOTSTRAP.md"
}

main "$@"
