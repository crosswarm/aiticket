#!/usr/bin/env python3
"""LLM 用量报表：把"每天调了多少次、哪个功能吃掉多少、配额报错说了什么"直接查出来。

在此之前这些只能靠 grep 日志反推，且已导致过两次错误结论：
把日志行数当调用数（3.9 次/工单，实际 1.9）、把一次性观测当常量（"配额 199/日"）。

用法::

    python3 scripts/llm_usage_report.py                 # 近 7 天
    python3 scripts/llm_usage_report.py --days 30
    python3 scripts/llm_usage_report.py --errors        # 只看失败（配额证据在这）

172 上::

    docker exec -w /app/APP/backend aiticket python3 scripts/llm_usage_report.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_metrics import summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 调用用量报表")
    parser.add_argument("--days", type=int, default=7, help="统计天数（默认 7）")
    parser.add_argument("--errors", action="store_true", help="只看失败记录")
    args = parser.parse_args()

    data = summary(days=args.days)
    if "error" in data:
        print(f"读取指标库失败：{data['error']}")
        print(f"路径：{data.get('db_path')}")
        return 1

    print(f"指标库：{data['db_path']}")
    print(f"累计记录：{data['total_records']} 条")

    if not args.errors:
        print(f"\n【近 {args.days} 天按日】")
        if data["by_day"]:
            print(f"  {'日期':<12}{'调用':>7}{'成功':>7}{'失败':>7}{'平均耗时':>10}")
            for row in data["by_day"]:
                print(f"  {row['day']:<12}{row['calls']:>7}{row['ok']:>7}"
                      f"{row['failed']:>7}{str(row['avg_ms']) + 'ms':>10}")
        else:
            print("  （无数据——埋点刚上线或这段时间没有调用）")

        print(f"\n【近 {args.days} 天按功能】")
        for row in data["by_feature"]:
            flag = f"  失败 {row['failed']}" if row["failed"] else ""
            print(f"  {row['feature']:<40}{row['calls']:>6} 次{flag}")

    errors = data["recent_errors"]
    print(f"\n【最近失败 {len(errors)} 条】配额数字就藏在报错体里")
    if not errors:
        print("  （无失败记录）")
    for row in errors:
        print(f"  {row['ts']}  {row['provider']}  {row['error_type']}")
        print(f"    {row['error_body']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
