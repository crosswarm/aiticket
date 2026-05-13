#!/usr/bin/env python3
"""
周报自动化：导出CSV → LLM分析 → 飞书通知

用法:
  python run_weekly_report.py             # 上周（默认）
  python run_weekly_report.py --this      # 本周
  python run_weekly_report.py --week-offset -2  # 两周前
  python run_weekly_report.py --dry-run   # 测试（不实际发飞书）
"""
import sys
import json
import argparse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "services"))

from export_weekly_csv import export_weekly_csv
from weekly_analysis import WeeklyAnalyzer
from feishu_notifier import FeishuNotifier

REPORT_BASE_URL = "http://ticket.spux.cn/report.html"


def run(week_offset: int = -1, dry_run: bool = False, project_key: str = "MYPROJECT",
        domain_modules=None) -> None:
    # Step 1: 导出 Jira 数据到 CSV
    print(f"\n[1/3] 导出 Jira 数据 ({project_key}, week_offset={week_offset})...")
    csv_path = export_weekly_csv(week_offset, project_key=project_key, domain_modules=domain_modules)
    print(f"  CSV: {csv_path.name}")

    # Step 2: 加载 LLM 配置，调用 WeeklyAnalyzer
    print("\n[2/3] 生成分析报告...")
    llm_config_path = BACKEND_DIR / "llm_config.json"
    with open(llm_config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    routing_path = BACKEND_DIR / "llm_feature_routing.json"
    routing = {}
    if routing_path.exists():
        try:
            with open(routing_path, encoding="utf-8") as rf:
                routing = json.load(rf)
        except Exception:
            pass
    # per-project routing key 优先，fallback 到全局 weekly_report
    provider = (routing.get(f"weekly_report_{project_key}")
                or routing.get("weekly_report")
                or routing.get("_default")
                or cfg.get("last_provider", "minimax"))
    pc = cfg.get(provider, {})

    analyzer = WeeklyAnalyzer(project_key, domain_modules=domain_modules)
    result = analyzer.run(
        api_key=pc.get("api_key", ""),
        provider="OpenAI/Custom",
        model_name=pc.get("model_name", ""),
        base_url=pc.get("base_url", ""),
        csv_filename=csv_path.name,
        force=False,
    )

    if not result or result.get("status") not in ("success", "exists"):
        print(f"[run_weekly_report] ❌ 分析失败: {result}")
        sys.exit(1)

    start = result["data_start_date"]
    end = result["data_end_date"]
    report_file = result["filename"]  # e.g. Weekly_Report_2026-03-30_2026-04-05.json

    total = result.get("total_tickets")
    if total is None:
        # Report already existed — read total from the saved JSON
        try:
            import json as _json
            report_subdir = "WeeklyReports" if project_key == "MYPROJECT" else f"WeeklyReports/{project_key}"
            report_path = PROJECT_ROOT / "conclusion" / report_subdir / report_file
            with open(report_path, encoding="utf-8") as _f:
                total = _json.load(_f).get("meta", {}).get("total_tickets", "—")
        except Exception:
            total = "—"
    status_label = "（已有报告，跳过重新生成）" if result.get("status") == "exists" else ""
    print(f"  报告: {report_file}{status_label}")

    # Step 3: 飞书通知
    report_url = f"{REPORT_BASE_URL}?report={report_file}"
    message = f"""📊 **流程中心周报已生成{status_label}**

**周期**: {start} 至 {end}
**工单总数**: {total} 条

🔗 [查看完整报告]({report_url})

---
_自动生成 | aiticket 周报系统_"""

    print(f"\n[3/3] 飞书通知...")
    if dry_run:
        print("[DRY RUN] 消息内容:\n" + message)
    else:
        notifier = FeishuNotifier()
        ok = notifier.send_message(message)
        print(f"  {'✅ 发送成功' if ok else '❌ 发送失败'}")

    # Step 4: KPI 重点客户动态 Seed
    try:
        from services.kpi_key_customer_seeder import seed_from_latest_reports
        seed_result = seed_from_latest_reports()
        import logging as _logging
        _logging.getLogger(__name__).info("[weekly_report] KPI customer seed: %s", seed_result)
        print(f"\n[KPI Seed] added={len(seed_result.get('added',[]))} retained={len(seed_result.get('retained',[]))} suggested_demote={len(seed_result.get('suggested_demote',[]))}")
    except Exception as _e:
        print(f"\n[KPI Seed] 跳过（{_e}）")


def main():
    parser = argparse.ArgumentParser(description="周报自动化：导出CSV → 分析 → 飞书")
    parser.add_argument("--this", dest="this_week", action="store_true", help="本周（默认上周）")
    parser.add_argument("--week-offset", type=int, default=None, help="周偏移量（负数=过去）")
    parser.add_argument("--dry-run", action="store_true", help="不实际发送飞书消息")
    parser.add_argument("--project", default="MYPROJECT", help="Jira 项目 key（默认 MYPROJECT）")
    parser.add_argument("--modules", default="", help="逗号分隔领域模块，为空=全部")
    args = parser.parse_args()

    if args.week_offset is not None:
        offset = args.week_offset
    elif args.this_week:
        offset = 0
    else:
        offset = -1

    mods = [m.strip() for m in args.modules.split(",") if m.strip()] if args.modules else None
    run(week_offset=offset, dry_run=args.dry_run, project_key=args.project, domain_modules=mods)


if __name__ == "__main__":
    main()
