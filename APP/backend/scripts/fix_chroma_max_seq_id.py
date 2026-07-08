#!/usr/bin/env python3
"""
ChromaDB max_seq_id 诊断/修复工具

问题背景：
  数据库从备份恢复或 embeddings_queue 被手动清空后，max_seq_id 表保留了
  历史最大值（如 65242），而当前 queue 实际最大值更小（如 14534）。
  ChromaDB 的 automatically_purge=True 会删除所有 seq_id < max_seq_id 的
  新入队条目，导致 add()/upsert() 无异常、无日志，但 count/get 不变。

触发条件：数据库从备份 restore、queue 被手动清空、跨环境迁移后。

修复原理：
  将所有 max_seq_id > queue_max 的行 clamp 到 floor = max(queue_max, 0)。
  ⚠️ 不能 clamp 到 -1：-1 会触发 chromadb InternalError（实测崩溃）。
  空 queue 时 floor=0，安全。

用法：
  python3 scripts/fix_chroma_max_seq_id.py              # 仅诊断
  python3 scripts/fix_chroma_max_seq_id.py --fix        # 修复（含交互确认）
  python3 scripts/fix_chroma_max_seq_id.py --fix --yes  # 无交互确认（CI/自动化）
  python3 scripts/fix_chroma_max_seq_id.py --fix --no-backup  # 不备份（不推荐）
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent
_BACKEND_DIR = _SCRIPTS_DIR.parent
_APP_DIR = _BACKEND_DIR.parent


def _default_db_path() -> str:
    data_root = os.environ.get("AITICKET_DATA_ROOT") or str(_APP_DIR / "data")
    return str(Path(data_root) / "chroma" / "ticket" / "chroma.sqlite3")


def _check_concurrent_writers(db_path: str) -> list[str]:
    """检测是否有其他进程持有 chroma.sqlite3 的文件锁（lsof）。"""
    try:
        result = subprocess.run(
            ["lsof", "-t", db_path],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        # 排除当前进程
        own_pid = str(os.getpid())
        return [p for p in pids if p != own_pid]
    except Exception:
        return []  # lsof 不可用时跳过


def diagnose(db_path: str) -> dict:
    """诊断 max_seq_id 状态，返回诊断结果。"""
    conn = sqlite3.connect(db_path)
    try:
        # queue 当前最大值（空 queue → -1）
        queue_max_row = conn.execute(
            "SELECT COALESCE(MAX(seq_id), -1) FROM embeddings_queue"
        ).fetchone()
        queue_max = queue_max_row[0] if queue_max_row else -1

        # 安全 floor：空 queue 时用 0 而非 -1
        floor = max(queue_max, 0)

        # 所有 segment 的 max_seq_id
        rows = conn.execute(
            "SELECT segment_id, seq_id FROM max_seq_id ORDER BY seq_id DESC"
        ).fetchall()

        leading = [(sid, seq) for sid, seq in rows if seq > floor]
        healthy = [(sid, seq) for sid, seq in rows if seq <= floor]

        return {
            "db_path": db_path,
            "queue_max": queue_max,
            "floor": floor,
            "all_rows": rows,
            "leading_rows": leading,
            "healthy_rows": healthy,
            "needs_fix": len(leading) > 0,
        }
    finally:
        conn.close()


def print_report(d: dict, verbose: bool = True):
    print(f"\n{'─' * 56}")
    print(f"  ChromaDB max_seq_id 诊断报告")
    print(f"{'─' * 56}")
    print(f"  DB 路径      : {d['db_path']}")
    print(f"  queue 当前最大: {d['queue_max']}（空=-1 表示 queue 已清空）")
    print(f"  修复 floor   : {d['floor']}")
    print(f"  全部段数量   : {len(d['all_rows'])}")
    print(f"  领先行（需修）: {len(d['leading_rows'])}")
    print(f"  健康行       : {len(d['healthy_rows'])}")

    if d['leading_rows']:
        print(f"\n  ⚠️  领先行（seq_id > floor={d['floor']}）：")
        for sid, seq in d['leading_rows'][:10]:
            print(f"    {sid[:36]}  seq={seq}  → clamp 到 {d['floor']}")
        if len(d['leading_rows']) > 10:
            print(f"    ... 共 {len(d['leading_rows'])} 行")
    else:
        print(f"\n  ✅ 无领先行，无需修复")
    print(f"{'─' * 56}\n")


def fix(db_path: str, yes: bool = False, backup: bool = True) -> int:
    """执行修复，返回修复行数（0=已健康）。"""
    d = diagnose(db_path)
    print_report(d)

    if not d['needs_fix']:
        return 0

    if backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{db_path}.bak_{ts}"
        shutil.copy2(db_path, bak)
        print(f"  已备份至: {bak}")

    if not yes:
        ans = input(f"  确认将 {len(d['leading_rows'])} 行 clamp 到 {d['floor']}? [y/N] ")
        if ans.strip().lower() != "y":
            print("  取消，未修改")
            return 0

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE max_seq_id SET seq_id = ? WHERE seq_id > ?",
            (d['floor'], d['floor'])
        )
        conn.commit()

        # 验证
        remaining = conn.execute(
            "SELECT COUNT(*) FROM max_seq_id WHERE seq_id > ? OR seq_id < 0",
            (d['floor'],)
        ).fetchone()[0]
        assert remaining == 0, f"修复后仍有 {remaining} 行异常"

        n = len(d['leading_rows'])
        print(f"  ✅ 已修复 {n} 行（seq_id → {d['floor']}）")
        return n
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="诊断/修复 ChromaDB max_seq_id 过旧导致新写入被立即 purge 的问题"
    )
    parser.add_argument("--db", default=None, help="chroma.sqlite3 路径（默认自动解析）")
    parser.add_argument("--fix", action="store_true", help="执行修复（默认只诊断）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（适用于 CI/脚本）")
    parser.add_argument("--no-backup", action="store_true", help="不备份（不推荐）")
    args = parser.parse_args()

    db_path = args.db or _default_db_path()

    # 前置守卫 1：server 模式禁止直接操作 SQLite
    if os.environ.get("CHROMA_MODE", "").lower() == "server":
        print("错误：CHROMA_MODE=server 模式下禁止直接操作 SQLite。")
        print("请先停止 Chroma daemon（supervisorctl stop chroma-board），再运行此工具。")
        sys.exit(1)

    # 前置守卫 2：检测并发写者
    if not args.fix:
        pass  # 诊断模式无写操作，跳过
    else:
        writers = _check_concurrent_writers(db_path)
        if writers:
            print(f"错误：检测到其他进程（PID {', '.join(writers)}）正在持有 {db_path}。")
            print("请先停止后端（supervisorctl stop ai-ticket / pkill uvicorn）再运行。")
            sys.exit(1)

    if not os.path.exists(db_path):
        print(f"错误：数据库文件不存在：{db_path}")
        sys.exit(1)

    if args.fix:
        n = fix(db_path, yes=args.yes, backup=not args.no_backup)
        sys.exit(0 if n >= 0 else 1)
    else:
        d = diagnose(db_path)
        print_report(d)
        if d['needs_fix']:
            print("  提示：运行 --fix 执行修复")
            sys.exit(2)  # 2 = 需要修复
        sys.exit(0)


if __name__ == "__main__":
    main()
