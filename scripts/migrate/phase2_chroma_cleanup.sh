#!/bin/bash
# Phase 2 终轮 · 旧 chroma 目录清理（24h 观察期后执行）
# 由 Jobmaster 在迁移 24h 后自动触发，也可手动运行
# 安全守则：先验证新目录存在且非空，再删除旧目录

set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

log "=== Phase 2 chroma 清理开始 ==="

# ── 1. 安全检查：新目录必须存在且非空 ──────────────────────────────────────
check_new() {
    local path="$ROOT/$1" label="$2"
    if [ ! -d "$path" ]; then
        die "新 chroma 目录不存在: $path (跳过清理，请检查迁移状态)"
    fi
    local count
    count=$(find "$path" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
    if [ "$count" -eq 0 ]; then
        die "新 chroma 目录为空: $path (跳过清理，可能迁移未完成)"
    fi
    log "✓ 新目录 $label OK ($count 个条目)"
}

check_new "APP/data/chroma/ticket"            "ticket chroma"
check_new "APP/data/chroma/kb"                "kb chroma"
check_new "APP/data/chroma/reply_trainer"     "reply_trainer chroma"

# ── 2. 删除旧 chroma 目录 ──────────────────────────────────────────────────
remove_old() {
    local path="$ROOT/$1"
    if [ -d "$path" ]; then
        log "删除旧目录: $1"
        rm -rf "$path"
    else
        log "已不存在(跳过): $1"
    fi
}

remove_old "APP/data/chroma_kb"
remove_old "APP/data/chroma_kb_runtime"
remove_old "APP/backend/chroma_db"
remove_old "APP/backend/data/reply_trainer/chroma"
remove_old "APP/backend/data/reply_trainer/chroma_runtime"
remove_old "APP/backend/data/competitor_kb/chroma"

# ── 3. 清理旧 APP/backend/data 空壳 ───────────────────────────────────────
for d in "APP/backend/data/reply_trainer" "APP/backend/data/competitor_kb" "APP/backend/data"; do
    if [ -d "$ROOT/$d" ] && [ -z "$(ls -A "$ROOT/$d" 2>/dev/null)" ]; then
        rmdir "$ROOT/$d"
        log "删除空目录: $d"
    fi
done

# ── 4. 重命名根 data/ 为 data.legacy.YYYYMMDD ──────────────────────────────
DATA_ROOT="$ROOT/data"
if [ -d "$DATA_ROOT" ]; then
    LEGACY="$ROOT/data.legacy.$(date +%Y%m%d)"
    mv "$DATA_ROOT" "$LEGACY"
    log "重命名: data/ → data.legacy.$(date +%Y%m%d)"
    # 把遗留目录加入 gitignore（防止误入 git）
    if ! grep -q "data.legacy" "$ROOT/.gitignore" 2>/dev/null; then
        echo -e "\n# Phase 2 终轮 · 旧 data/ 重命名后遗留\ndata.legacy.*/" >> "$ROOT/.gitignore"
    fi
else
    log "根 data/ 目录不存在，已跳过重命名"
fi

# ── 5. 最终验证 ────────────────────────────────────────────────────────────
log "=== 清理完成，验证新 chroma 状态 ==="
du -sh "$ROOT/APP/data/chroma/"* 2>/dev/null | while read -r line; do log "$line"; done

log "=== Phase 2 chroma 清理成功结束 ==="
