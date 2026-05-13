#!/usr/bin/env python3
"""
定时任务监控看门狗 — 检查所有定时任务是否按时完成，未完成的自动补救 + 飞书告警。
每天 10:00 运行。

检查项：
  1. 周报（周日 09:00 应完成）
  2. 月报（月末 21:00 应完成）
  3. 训练器（每天 02:00）
  4. KB enricher（每天 03:00）
  5. Agent A indexer（每 2h）
"""
import sys, os, json, subprocess, calendar
from pathlib import Path
from datetime import datetime, date, timedelta

for _h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
    _e = os.environ.get("no_proxy", "")
    if _h not in _e:
        os.environ["no_proxy"] = f"{_e},{_h}".strip(",")
os.environ["NO_PROXY"] = os.environ.get("no_proxy", "")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent
sys.path.insert(0, str(BACKEND_DIR))

PYTHON = "/Volumes/MacMini/opt/miniconda3/envs/antigravity/bin/python"
FEISHU_CHAT_ID = "oc_72ef8553bb8b552435cd91b0fb1e86ab"


def _notify(message: str):
    """飞书告警"""
    try:
        sys.path.insert(0, str(BACKEND_DIR / "services"))
        from feishu_notifier import FeishuNotifier
        FeishuNotifier().send_message(message, chat_id=FEISHU_CHAT_ID)
        print(f"[飞书] 已推送告警")
    except Exception as e:
        print(f"[飞书] 推送失败: {e}")


def _run_script(script_path: str, args: list = None) -> tuple:
    """运行补救脚本，返回 (success, output)"""
    cmd = [PYTHON, script_path] + (args or [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(BACKEND_DIR))
        return r.returncode == 0, (r.stdout + r.stderr)[-500:]
    except Exception as e:
        return False, str(e)


def check_weekly_report() -> dict:
    """检查本周的周报是否已生成（周日检查）"""
    today = date.today()
    if today.weekday() != 6:  # 只在周日检查
        return {"name": "周报", "status": "skip", "reason": "非周日"}

    # 计算本周的日期范围
    week_start = today - timedelta(days=6)  # 上周一
    week_end = today - timedelta(days=0)    # 本周日

    reports_dir = PROJECT_ROOT / "conclusion" / "WeeklyReports"
    if not reports_dir.exists():
        return {"name": "周报", "status": "missing", "reason": "WeeklyReports 目录不存在"}

    # 检查是否有包含本周日期的报告
    expected_pattern = week_start.strftime("%Y-%m-%d")
    found = any(expected_pattern in f.name for f in reports_dir.glob("*.json"))
    if found:
        return {"name": "周报", "status": "ok"}

    # 尝试补跑
    print("[补救] 周报未生成，尝试补跑...")
    ok, output = _run_script(str(SCRIPT_DIR / "run_weekly_report.py"))
    return {
        "name": "周报",
        "status": "remedied" if ok else "failed",
        "reason": f"自动补跑{'成功' if ok else '失败'}",
        "detail": output[-200:] if not ok else "",
    }


def check_monthly_report() -> dict:
    """检查上月月报（每月 1 号检查）"""
    today = date.today()
    if today.day != 1:
        return {"name": "月报", "status": "skip", "reason": "非月初"}

    last_month = today.month - 1 if today.month > 1 else 12
    last_year = today.year if today.month > 1 else today.year - 1
    expected_prefix = f"Monthly_Report_{last_year}{last_month:02d}"

    reports_dir = PROJECT_ROOT / "conclusion" / "MonthlyReports"
    found = any(expected_prefix in f.name for f in reports_dir.glob("*.json")) if reports_dir.exists() else False
    if found:
        return {"name": "月报", "status": "ok"}

    print(f"[补救] {last_year}年{last_month}月月报未生成，尝试补跑...")
    ok, output = _run_script(
        str(SCRIPT_DIR / "run_monthly_report.py"),
        ["--year", str(last_year), "--month", str(last_month), "--force"]
    )
    return {
        "name": "月报",
        "status": "remedied" if ok else "failed",
        "reason": f"自动补跑{'成功' if ok else '失败'}",
        "detail": output[-200:] if not ok else "",
    }


def check_trainer() -> dict:
    """检查训练器是否在最近 26 小时内运行过"""
    metrics_file = PROJECT_ROOT / "conclusion" / "training" / "training_metrics.jsonl"
    if not metrics_file.exists():
        return {"name": "训练器", "status": "missing", "reason": "training_metrics.jsonl 不存在"}
    try:
        last_line = metrics_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        ts = json.loads(last_line).get("timestamp", "")
        last_run = datetime.fromisoformat(ts)
        hours_ago = (datetime.now() - last_run).total_seconds() / 3600
        if hours_ago <= 26:
            return {"name": "训练器", "status": "ok", "reason": f"最近 {hours_ago:.0f}h 前运行"}
        return {"name": "训练器", "status": "stale", "reason": f"已 {hours_ago:.0f}h 未运行（超过 26h 阈值）"}
    except Exception as e:
        return {"name": "训练器", "status": "error", "reason": str(e)}


def check_kb_enricher() -> dict:
    """检查 KB enricher 是否在最近 26 小时内运行过"""
    log_file = BACKEND_DIR / "data" / "kb_enrichment_log.jsonl"
    if not log_file.exists():
        return {"name": "KB enricher", "status": "skip", "reason": "尚未首次运行"}
    try:
        last_line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        ts = json.loads(last_line).get("extracted_at", "")
        last_run = datetime.fromisoformat(ts)
        hours_ago = (datetime.now() - last_run).total_seconds() / 3600
        if hours_ago <= 26:
            return {"name": "KB enricher", "status": "ok", "reason": f"最近 {hours_ago:.0f}h 前"}
        return {"name": "KB enricher", "status": "stale", "reason": f"已 {hours_ago:.0f}h 未运行"}
    except Exception as e:
        return {"name": "KB enricher", "status": "error", "reason": str(e)}


def check_agent_a_indexer() -> dict:
    """检查 Agent A indexer 是否在最近 4 小时内运行过"""
    state_file = PROJECT_ROOT / "conclusion" / "training" / "agent_a_index_state.json"
    if not state_file.exists():
        return {"name": "Agent A indexer", "status": "missing"}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state.get("completed"):
            return {"name": "Agent A indexer", "status": "ok", "reason": "已全部完成"}
        last_run = state.get("last_run_at", "")
        last = datetime.fromisoformat(last_run)
        hours_ago = (datetime.now() - last).total_seconds() / 3600
        if hours_ago <= 4:
            pct = round(state.get("processed", 0) / max(state.get("total", 1), 1) * 100, 1)
            return {"name": "Agent A indexer", "status": "ok", "reason": f"进度 {pct}%，{hours_ago:.0f}h 前"}
        return {"name": "Agent A indexer", "status": "stale", "reason": f"已 {hours_ago:.0f}h 未运行"}
    except Exception as e:
        return {"name": "Agent A indexer", "status": "error", "reason": str(e)}


def main():
    print(f"\n{'='*60}")
    print(f"  定时任务看门狗 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    checks = [
        check_weekly_report(),
        check_monthly_report(),
        check_trainer(),
        check_kb_enricher(),
        check_agent_a_indexer(),
    ]

    alerts = []
    for c in checks:
        status = c["status"]
        icon = {"ok": "✅", "skip": "⏭", "remedied": "🔧", "failed": "❌", "stale": "⚠️", "missing": "❓", "error": "💥"}.get(status, "❓")
        reason = c.get("reason", "")
        print(f"  {icon} {c['name']}: {status} {reason}")
        if status in ("failed", "stale", "error", "missing"):
            alerts.append(c)

    if alerts:
        msg = f"⚠️ 定时任务异常告警 — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        for a in alerts:
            msg += f"❌ {a['name']}: {a.get('reason','')}\n"
            if a.get("detail"):
                msg += f"   详情: {a['detail'][:100]}\n"
        msg += f"\n共 {len(alerts)} 项异常，请检查"
        _notify(msg)
    else:
        active = [c for c in checks if c["status"] != "skip"]
        if active:
            print(f"\n✅ 全部 {len(active)} 项检查通过，无需告警")

    # 对补救成功的也推送通知
    remedied = [c for c in checks if c["status"] == "remedied"]
    if remedied:
        msg = f"🔧 定时任务自动补救 — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        for r in remedied:
            msg += f"✅ {r['name']}: {r.get('reason','')}\n"
        _notify(msg)


if __name__ == "__main__":
    main()
