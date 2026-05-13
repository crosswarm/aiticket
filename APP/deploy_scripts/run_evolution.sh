#!/bin/bash
# Darwin 进化框架每日自动运行
# 触发方式：launchd com.aiticket.evolution 每日 02:30
# 或手动：bash APP/deploy_scripts/run_evolution.sh

set -euo pipefail
cd /Users/cfone/Studio/aiticket

PY=/Volumes/MacMini/opt/miniconda3/envs/antigravity/bin/python3
LOG_DIR=conclusion/_local/evolution/logs
mkdir -p "$LOG_DIR"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/run-$TODAY.log"

echo "=== Darwin Evolution Daily Run: $TODAY ===" | tee "$LOG_FILE"
echo "Started: $(date)" | tee -a "$LOG_FILE"

# M2: 分类模块 — 快速评分 + 最多 3 轮突变
echo "" | tee -a "$LOG_FILE"
echo "--- classify ---" | tee -a "$LOG_FILE"
PYTHONPATH=APP/backend $PY -m evolution_core classify run \
    --max-rounds 3 --fast --report 2>&1 | tee -a "$LOG_FILE"

# M3: 回复模块 — 快速评分 + 最多 2 轮突变
echo "" | tee -a "$LOG_FILE"
echo "--- reply ---" | tee -a "$LOG_FILE"
PYTHONPATH=APP/backend $PY -m evolution_core reply run \
    --max-rounds 2 --fast --report 2>&1 | tee -a "$LOG_FILE"

# M5: 需求池/竞品分析 — 快速评分 + 最多 2 轮突变
echo "" | tee -a "$LOG_FILE"
echo "--- reqpool ---" | tee -a "$LOG_FILE"
PYTHONPATH=APP/backend $PY -m evolution_core reqpool run \
    --max-rounds 2 --fast --report 2>&1 | tee -a "$LOG_FILE"

echo "" | tee -a "$LOG_FILE"
echo "Finished: $(date)" | tee -a "$LOG_FILE"

# 汇总今日结果
echo "" | tee -a "$LOG_FILE"
echo "--- 今日结果 ---" | tee -a "$LOG_FILE"
PYTHONPATH=APP/backend $PY -c "
from evolution_core.ledger import Ledger
from datetime import date
today = date.today().isoformat()
for mod in ['classify', 'reply', 'reqpool']:
    ledger = Ledger(mod)
    rows = ledger.tail(5)
    today_rows = [r for r in rows if r.get('timestamp','').startswith(today)]
    if today_rows:
        kept = sum(1 for r in today_rows if r.get('kept') == '1')
        print(f'{mod}: {len(today_rows)} rounds today, {kept} kept')
    else:
        print(f'{mod}: no rounds today')
" 2>&1 | tee -a "$LOG_FILE"

echo "Log: $LOG_FILE"
