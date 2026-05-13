#!/usr/bin/env python3
"""
自动采集周报/月报数据 — 替代手动 Jira CSV 导出
BIP应用与开发平台产品规划部 qiangxiao, 2026

用法:
  python auto_report_data.py --weekly              # 导出本周数据
  python auto_report_data.py --weekly --last        # 导出上周数据
  python auto_report_data.py --monthly              # 导出本月数据
  python auto_report_data.py --monthly --last        # 导出上月数据
  python auto_report_data.py --yoy-check             # 检查/生成去年同比数据
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
SKILL_SCRIPT = PROJECT_ROOT / ".agent" / "skills" / "ticket-query" / "scripts" / "jira_query.py"


def get_week_range(offset=0):
    """获取周范围 (周一 ~ 周日)"""
    today = datetime.now()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def get_month_range(offset=0):
    """获取月范围"""
    today = datetime.now()
    year = today.year
    month = today.month + offset
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    start = f"{year}-{month:02d}-01"
    # 下月1号
    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1
    end = f"{next_year}-{next_month:02d}-01"
    return start, end


def run_jira_query(jql, output_file, use_report_csv=True):
    """调用 jira_query.py 执行查询并导出"""
    cmd = [
        sys.executable, str(SKILL_SCRIPT),
        "--jql", jql,
        "--all",
    ]
    if use_report_csv:
        cmd.extend(["--report-csv", str(output_file)])
    else:
        cmd.extend(["--csv", str(output_file)])

    print(f"  JQL: {jql}")
    print(f"  输出: {output_file}")
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


def _merge_csvs(main_csv, extra_csv, output_csv):
    """合并两个CSV，按问题关键字去重"""
    import pandas as pd
    dfs = []
    for p in [main_csv, extra_csv]:
        if p.exists() and p.stat().st_size > 0:
            try:
                df = pd.read_csv(p)
                if len(df) > 0:
                    dfs.append(df)
            except Exception:
                pass
    if not dfs:
        return False
    combined = pd.concat(dfs, ignore_index=True)
    combined.columns = [c.strip() for c in combined.columns]
    if '问题关键字' in combined.columns:
        combined = combined.drop_duplicates(subset=['问题关键字'])
    combined.to_csv(output_csv, index=False)
    return True


def export_weekly(last=False):
    """导出周数据 (MYPROJECT核心 + 转出工单)"""
    offset = -1 if last else 0
    start, end = get_week_range(offset)
    label = "上周" if last else "本周"
    print(f"\n📊 导出{label}数据 ({start} ~ {end})")

    ts = datetime.now().strftime("%Y-%m-%dT%H_%M_%S+0800")
    filename = f"工作流-周数据-{start}-{end}T{ts.split('T')[1]}.csv"
    output = SRC_DIR / filename

    # 1. MYPROJECT核心工单
    jql_core = (
        f'project = MYPROJECT AND issuetype = "支持问题" '
        f'AND created >= "{start}" AND created < "{end}"'
    )
    tmp_core = SRC_DIR / f"_tmp_core_{start}.csv"
    ok_core = run_jira_query(jql_core, tmp_core)

    # 2. 转出工单: 初始项目=流程中心，当前已不在MYPROJECT
    jql_transferred = (
        f'issuetype = "支持问题" AND cf[11935] = "云平台-流程中心" '
        f'AND project != MYPROJECT '
        f'AND created >= "{start}" AND created < "{end}"'
    )
    tmp_trans = SRC_DIR / f"_tmp_trans_{start}.csv"
    ok_trans = run_jira_query(jql_transferred, tmp_trans)

    # 3. 合并去重
    if ok_core or ok_trans:
        _merge_csvs(tmp_core, tmp_trans, output)
        # 统计
        import pandas as pd
        df = pd.read_csv(output)
        df.columns = [c.strip() for c in df.columns]
        total = len(df)
        lczx = len(df[df.get('项目名称', df.get('项目关键字', pd.Series())).astype(str).str.contains('流程中心', na=False)]) if '项目名称' in df.columns else total
        transferred = total - lczx
        print(f"\n✅ 周数据已导出: {output.name} (总计{total}条: 核心{lczx} + 转出{transferred})")
    else:
        print(f"\n❌ 导出失败")

    # 清理临时文件
    for tmp in [tmp_core, tmp_trans]:
        if tmp.exists():
            tmp.unlink()


def export_monthly(last=False):
    """导出月数据 (MYPROJECT核心 + 转出工单)"""
    offset = -1 if last else 0
    start, end = get_month_range(offset)
    label = "上月" if last else "本月"
    print(f"\n📊 导出{label}数据 ({start} ~ {end})")

    ts = datetime.now().strftime("%Y-%m-%dT%H_%M_%S+0800")
    filename = f"工作流-月数据-{start[:7]}T{ts.split('T')[1]}.csv"
    output = SRC_DIR / filename

    # 1. MYPROJECT核心工单
    jql_core = (
        f'project = MYPROJECT AND issuetype = "支持问题" '
        f'AND created >= "{start}" AND created < "{end}"'
    )
    tmp_core = SRC_DIR / f"_tmp_core_{start[:7]}.csv"
    ok_core = run_jira_query(jql_core, tmp_core)

    # 2. 转出工单
    jql_transferred = (
        f'issuetype = "支持问题" AND cf[11935] = "云平台-流程中心" '
        f'AND project != MYPROJECT '
        f'AND created >= "{start}" AND created < "{end}"'
    )
    tmp_trans = SRC_DIR / f"_tmp_trans_{start[:7]}.csv"
    ok_trans = run_jira_query(jql_transferred, tmp_trans)

    # 3. 合并去重
    if ok_core or ok_trans:
        _merge_csvs(tmp_core, tmp_trans, output)
        import pandas as pd
        df = pd.read_csv(output)
        df.columns = [c.strip() for c in df.columns]
        total = len(df)
        lczx = len(df[df.get('项目名称', pd.Series()).astype(str).str.contains('流程中心', na=False)]) if '项目名称' in df.columns else total
        transferred = total - lczx
        print(f"\n✅ 月数据已导出: {output.name} (总计{total}条: 核心{lczx} + 转出{transferred})")
    else:
        print(f"\n❌ 导出失败")

    # 清理临时文件
    for tmp in [tmp_core, tmp_trans]:
        if tmp.exists():
            tmp.unlink()


def check_yoy_data():
    """检查并生成去年同比数据"""
    last_year = datetime.now().year - 1
    pattern = str(SRC_DIR / f"工作流-{last_year}完成*")
    existing = glob.glob(pattern)

    if existing:
        print(f"\n✅ 已存在 {last_year} 年完成数据:")
        for f in existing:
            print(f"  {os.path.basename(f)}")
        print("  无需重新查询")
        return

    print(f"\n⚠️  未找到 {last_year} 年完成数据，从 Jira 查询...")
    filename = f"工作流-{last_year}完成-auto.csv"
    output = SRC_DIR / filename

    jql = (
        f'project = MYPROJECT AND issuetype = "支持问题" '
        f'AND created >= "{last_year}-01-01" AND created < "{last_year + 1}-01-01"'
    )
    if run_jira_query(jql, output):
        print(f"\n✅ 同比数据已导出: {filename}")
    else:
        print(f"\n❌ 导出失败")


def is_last_day_of_month():
    """判断今天是否为当月最后一天"""
    import calendar
    today = datetime.now()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day == last_day


def sync_to_qcl(report_type="weekly"):
    """推送报告数据到QCL服务器"""
    sync_script = PROJECT_ROOT / "APP" / "deploy_scripts" / "sync_report_data.sh"
    if not sync_script.exists():
        print("  ⚠️ sync_report_data.sh 不存在，跳过QCL推送")
        return False

    flag = f"--{report_type}" if report_type in ("weekly", "monthly") else ""
    cmd = ["bash", str(sync_script)]
    if flag:
        cmd.append(flag)

    print(f"\n📤 推送{report_type}数据到QCL...")
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


def notify_feishu(report_type, summary):
    """发送飞书通知"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "APP" / "backend"))
        from services.feishu_notifier import FeishuNotifier
        notifier = FeishuNotifier()
        msg = f"📊 {report_type}数据已自动采集并推送\n{summary}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        if notifier.send_message(msg):
            print("  ✅ 飞书通知已发送")
        else:
            print("  ⚠️ 飞书通知发送失败")
    except Exception as e:
        print(f"  ⚠️ 飞书通知异常: {e}")


def main():
    parser = argparse.ArgumentParser(description="自动采集周报/月报数据")
    parser.add_argument("--weekly", action="store_true", help="导出周数据")
    parser.add_argument("--monthly", action="store_true", help="导出月数据")
    parser.add_argument("--last", action="store_true", help="导出上一期（上周/上月）")
    parser.add_argument("--yoy-check", action="store_true", help="检查/生成去年同比数据")
    parser.add_argument("--last-day-only", action="store_true",
                        help="仅在当月最后一天执行 (配合launchd 28-31日触发使用)")
    parser.add_argument("--no-sync", action="store_true", help="跳过QCL推送")
    parser.add_argument("--no-notify", action="store_true", help="跳过飞书通知")
    args = parser.parse_args()

    if not any([args.weekly, args.monthly, args.yoy_check]):
        parser.error("请指定 --weekly、--monthly 或 --yoy-check")

    # 月报配合launchd: 28-31日每天触发，仅最后一天真正执行
    if args.last_day_only and not is_last_day_of_month():
        today = datetime.now()
        print(f"⏭️  今天是{today.month}月{today.day}日，不是月末最后一天，跳过")
        return

    if args.weekly:
        export_weekly(last=args.last)
        if not args.no_sync:
            sync_to_qcl("weekly")
        if not args.no_notify:
            offset = -1 if args.last else 0
            start, end = get_week_range(offset)
            notify_feishu("周报", f"数据范围: {start} ~ {end}")

    if args.monthly:
        export_monthly(last=args.last)
        if not args.no_sync:
            sync_to_qcl("monthly")
        if not args.no_notify:
            offset = -1 if args.last else 0
            start, end = get_month_range(offset)
            notify_feishu("月报", f"数据范围: {start} ~ {end}")

    if args.yoy_check:
        check_yoy_data()


if __name__ == "__main__":
    main()
