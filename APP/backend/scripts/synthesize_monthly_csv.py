#!/usr/bin/env python3
"""
synthesize_monthly_csv.py
合成指定月份的月度工单 CSV，供 monthly_analysis.py 使用。

用法:
  python scripts/synthesize_monthly_csv.py --year 2026 --month 4

逻辑:
  1. 扫描 src/ 目录，找所有含「周数据」的 CSV
  2. 从文件名提取 [week_start, week_end]，凡与目标月有交集的均纳入
  3. 同周区间多版本取最新 mtime（防 600 字节空文件）
  4. concat + 按 创建日期 过滤到目标月
  5. 按 问题关键字 去重（keep='last'）
  6. 输出 src/工作流-月数据-{YYYYMM}-{timestamp}.csv
"""

import argparse
import calendar
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# 项目根: scripts/ → backend/ → APP/ → root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SRC = _PROJECT_ROOT / "src"

DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')


def _to_date(match_tuple):
    return date(int(match_tuple[0]), int(match_tuple[1]), int(match_tuple[2]))


def _modules_slug(domain_modules=None) -> str:
    if not domain_modules:
        return ""
    import hashlib
    return "_" + hashlib.md5(",".join(sorted(domain_modules)).encode()).hexdigest()[:6]


def find_weekly_csvs_for_month(year: int, month: int, project_key: str = "MYPROJECT",
                                domain_modules=None):
    """返回与 [month_start, month_end] 有交集的周 CSV，同周区间取最新 mtime。"""
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    slug = _modules_slug(domain_modules)
    csv_prefix = f"{project_key}{slug}-周数据"

    # (week_start, week_end) -> (mtime, Path)
    best: dict = {}

    for f in _SRC.iterdir():
        if not f.name.endswith('.csv') or '周数据' not in f.name:
            continue
        # 只取当前 project+slug 的周 CSV
        if not f.name.startswith(csv_prefix):
            continue

        matches = DATE_RE.findall(f.name)
        if not matches:
            continue

        ws = _to_date(matches[0])
        we = _to_date(matches[-1]) if len(matches) >= 2 else ws + timedelta(days=6)

        if we < month_start or ws > month_end:
            continue

        key = (ws, we)
        mtime = f.stat().st_mtime
        if key not in best or mtime > best[key][0]:
            best[key] = (mtime, f)

    return [path for _, path in sorted(best.values(), key=lambda x: x[1].name)]


def synthesize(year: int, month: int) -> Path:
    if not _SRC.exists():
        print(f"ERROR: src/ 目录不存在: {_SRC}", file=sys.stderr)
        sys.exit(1)

    csv_files = find_weekly_csvs_for_month(year, month)
    if not csv_files:
        print(f"WARN: {year}-{month:02d} 无有交集的周 CSV", file=sys.stderr)
        sys.exit(1)

    print(f"[synthesize] {year}-{month:02d} 找到 {len(csv_files)} 份周 CSV:")
    dfs = []
    for p in csv_files:
        print(f"  {p.name}")
        try:
            df = pd.read_csv(p)
            df.columns = [c.strip() for c in df.columns]
            if '创建日期' not in df.columns:
                print(f"  SKIP (缺 创建日期): {p.name}", file=sys.stderr)
                continue
            if '问题关键字' not in df.columns:
                print(f"  SKIP (缺 问题关键字): {p.name}", file=sys.stderr)
                continue
            dfs.append(df)
        except Exception as e:
            print(f"  SKIP (读取失败 {e}): {p.name}", file=sys.stderr)

    if not dfs:
        print("ERROR: 无有效 CSV 可合并", file=sys.stderr)
        sys.exit(1)

    combined = pd.concat(dfs, ignore_index=True)
    combined['_created'] = pd.to_datetime(combined['创建日期'], errors='coerce')

    # 过滤到目标月
    mask = (combined['_created'].dt.year == year) & (combined['_created'].dt.month == month)
    filtered = combined[mask].copy()
    filtered.drop(columns=['_created'], inplace=True)

    # 按 问题关键字 去重
    filtered.drop_duplicates(subset=['问题关键字'], keep='last', inplace=True)
    filtered.reset_index(drop=True, inplace=True)

    if len(filtered) == 0:
        print(f"WARN: 过滤后 {year}-{month:02d} 无工单（检查 创建日期 列格式）", file=sys.stderr)

    print(f"[synthesize] 过滤后目标月工单数: {len(filtered)}")

    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    out_name = f"工作流-月数据-{year}{month:02d}-{ts}.csv"
    out_path = _SRC / out_name
    filtered.to_csv(out_path, index=False, encoding='utf-8-sig')
    print(f"[synthesize] 输出: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description='合成月度工单 CSV')
    parser.add_argument('--year', type=int, required=True, help='年份，例如 2026')
    parser.add_argument('--month', type=int, required=True, help='月份 1-12')
    args = parser.parse_args()

    if not (1 <= args.month <= 12):
        print("ERROR: --month 必须在 1-12 范围内", file=sys.stderr)
        sys.exit(1)

    synthesize(args.year, args.month)


if __name__ == '__main__':
    main()
