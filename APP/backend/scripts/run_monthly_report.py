#!/usr/bin/env python3
"""
月报自动化：在月末最后一天 21:00 自动生成当月月报。

LaunchAgent 每月 28-31 号都触发，脚本内判断是否是当月最后一天。
非最后一天则静默退出。

用法:
  python run_monthly_report.py              # 自动判断（仅月末执行）
  python run_monthly_report.py --force      # 强制生成当月月报
  python run_monthly_report.py --month 3    # 生成指定月份
  python run_monthly_report.py --dry-run    # 测试（不发飞书）
"""
import sys, os, json, argparse, calendar
from pathlib import Path
from datetime import datetime, date

# no_proxy
for _h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
    _e = os.environ.get("no_proxy", "")
    if _h not in _e:
        os.environ["no_proxy"] = f"{_e},{_h}".strip(",")
os.environ["NO_PROXY"] = os.environ.get("no_proxy", "")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "services"))


def is_last_day_of_month(d: date = None) -> bool:
    d = d or date.today()
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.day == last_day


def run(year: int = None, month: int = None, force: bool = False, dry_run: bool = False,
        project_key: str = "MYPROJECT", domain_modules=None):
    today = date.today()
    year = year or today.year
    month = month or today.month

    if not force and not is_last_day_of_month(today):
        print(f"[月报] 今天 {today} 不是月末最后一天，跳过（用 --force 强制）")
        return

    print(f"\n{'='*60}")
    print(f"  月报自动生成 — {project_key} {year}年{month}月")
    print(f"{'='*60}\n")

    # Step 1: 加载 LLM 配置
    print("[1/3] 加载配置...")
    llm_config_path = BACKEND_DIR / "llm_config.json"
    with open(llm_config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    # Fix R3: 不再硬走 last_provider(minimax)，改用降级链（zhipu→minimax）
    # 避免 minimax 429 时无任何兜底
    try:
        sys.path.insert(0, str(BACKEND_DIR / "services"))
        from local_llm_lifecycle import with_fallback_chain
        provider = with_fallback_chain("monthly_report", ["zhipu", "minimax"])
        print(f"  LLM 降级链选定: {provider}")
    except Exception as _lc_err:
        provider = cfg.get("last_provider", "zhipu")
        print(f"  with_fallback_chain 不可用({_lc_err})，fallback: {provider}")
    # 必须按新 provider 重新取 pcfg（含正确的 api_key/base_url/model_name）
    pcfg = cfg.get(provider, {})

    # Step 2: 生成月报
    print("[2/3] 生成月报...")
    from monthly_analysis import MonthlyReportGenerator
    generator = MonthlyReportGenerator(project_key, domain_modules=domain_modules)
    result = generator.generate(
        year=year, month=month, force=force,
        api_key=pcfg.get("api_key", ""),
        provider=provider,
        model_name=pcfg.get("model_name", ""),
        base_url=pcfg.get("base_url", ""),
    )

    if not result:
        print("[月报] 生成失败（无数据或已存在）")
        return

    report_path = result.get("json_path") or result.get("md_path", "")
    print(f"  报告: {report_path}")

    # Step 3: 飞书通知
    if not dry_run:
        print("[3/3] 飞书通知...")
        try:
            from feishu_notifier import FeishuNotifier
            notifier = FeishuNotifier()
            msg = (
                f"📊 {year}年{month}月 月度分析报告已生成\n\n"
                f"📖 查看: http://ticket.spux.cn/report.html\n"
                f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
            notifier.send_message(msg)
            print("  ✅ 飞书通知已发送")
        except Exception as e:
            print(f"  ⚠️ 飞书通知失败: {e}")
    else:
        print("[3/3] DRY-RUN: 跳过飞书通知")

    # KPI 重点客户动态 Seed
    try:
        from services.kpi_key_customer_seeder import seed_from_latest_reports
        seed_result = seed_from_latest_reports()
        print(f"\n[KPI Seed] added={len(seed_result.get('added',[]))} retained={len(seed_result.get('retained',[]))} suggested_demote={len(seed_result.get('suggested_demote',[]))}")
    except Exception as _e:
        print(f"\n[KPI Seed] 跳过（{_e}）")

    print(f"\n✅ {year}年{month}月 月报生成完成")


def main():
    parser = argparse.ArgumentParser(description="月报自动生成")
    parser.add_argument("--year", type=int, help="年份")
    parser.add_argument("--month", type=int, help="月份")
    parser.add_argument("--force", action="store_true", help="强制生成（不判断是否月末）")
    parser.add_argument("--dry-run", action="store_true", help="测试模式")
    parser.add_argument("--project", default="MYPROJECT", help="Jira 项目 key（默认 MYPROJECT）")
    parser.add_argument("--modules", default="", help="逗号分隔领域模块，为空=全部")
    args = parser.parse_args()
    mods = [m.strip() for m in args.modules.split(",") if m.strip()] if args.modules else None
    run(year=args.year, month=args.month, force=args.force, dry_run=args.dry_run,
        project_key=args.project, domain_modules=mods)


if __name__ == "__main__":
    main()
