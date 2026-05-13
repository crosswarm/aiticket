#!/bin/bash
# mini 本地环境回退脚本
# 用于回退到升级前状态
# 注意: jira_proxy 使用端口 5001 (端口 5000 被 macOS AirPlay Receiver 占用)

set -e

echo "=========================================="
echo "   mini 环境回退脚本"
echo "=========================================="
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 项目路径
PROJECT_DIR="/Volumes/MacMini/Users/cfone/Documents/用友/AI工单"
BACKEND_DIR="$PROJECT_DIR/APP/backend"
PROXY_PORT="${1:-5001}"

# 切换到项目目录
cd "$PROJECT_DIR"

# 获取预升级标签
PRE_UPGRADE_TAG=$(git tag -l "*-pre-upgrade" | tail -1)

if [ -z "$PRE_UPGRADE_TAG" ]; then
    echo -e "${YELLOW}未找到预升级标签${NC}"
    echo "可用的标签:"
    git tag -l
else
    echo "找到预升级标签: $PRE_UPGRADE_TAG"
fi
echo ""

# 1. 停止新服务
echo -e "${YELLOW}步骤 1: 停止新服务...${NC}"
if pgrep -f "jira_proxy.py" > /dev/null; then
    pkill -f "jira_proxy.py"
    echo "✓ 已停止 jira_proxy 服务"
else
    echo "○ jira_proxy 服务未运行"
fi

# 等待进程完全停止
sleep 2

# 2. 验证端口
echo -e "${YELLOW}步骤 2: 验证端口状态...${NC}"
if lsof -i :$PROXY_PORT > /dev/null 2>&1; then
    echo -e "${RED}✗ 端口 $PROXY_PORT 仍被占用${NC}"
else
    echo "✓ 端口 $PROXY_PORT 已释放"
fi

# 3. 确认主服务状态
echo -e "${YELLOW}步骤 3: 确认主服务状态...${NC}"
if pgrep -f "main.py" > /dev/null; then
    echo "✓ 主服务 (main.py) 正在运行"
else
    echo -e "${YELLOW}○ 主服务未运行${NC}"
fi

# 4. 询问是否 Git 回退
if [ -n "$PRE_UPGRADE_TAG" ]; then
    echo ""
    echo -e "${YELLOW}是否执行 Git 回退到 $PRE_UPGRADE_TAG?${NC}"
    read -p "[y/N]: " git_rollback

    if [ "$git_rollback" = "y" ] || [ "$git_rollback" = "Y" ]; then
        echo ""
        echo -e "${YELLOW}步骤 4: Git 回退...${NC}"
        git checkout "$PRE_UPGRADE_TAG"
        echo -e "${GREEN}✓ 已回退到 $PRE_UPGRADE_TAG${NC}"

        # 提示重启主服务
        echo ""
        echo -e "${YELLOW}请重启主服务:${NC}"
        echo "  cd $BACKEND_DIR"
        echo "  python main.py"
    fi
fi

# 5. 回退检查清单
echo ""
echo "=========================================="
echo "   回退检查清单"
echo "=========================================="
echo ""

echo -n "[ ] jira_proxy 进程已停止: "
if pgrep -f "jira_proxy.py" > /dev/null; then
    echo -e "${RED}✗${NC}"
else
    echo -e "${GREEN}✓${NC}"
fi

echo -n "[ ] 主服务 (端口 8000) 仍在运行: "
if curl -s http://localhost:3000/api/board > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${YELLOW}○${NC}"
fi

echo -n "[ ] 前端访问正常: "
echo "请手动验证 (http://localhost:3000/)"
echo ""
echo -n "[ ] 功能回归测试通过: "
echo "请手动验证"

echo ""
echo "=========================================="
echo "   回退完成"
echo "=========================================="
