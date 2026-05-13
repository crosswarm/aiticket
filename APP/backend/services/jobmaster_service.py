"""
JobMasterService — REST 聚合层

只读聚合 schedules/*.json + jobmaster_state.json + agent_tasks SQLite + events.jsonl。
不修改任何现有文件，JobMaster CLI 保持原样运行。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from croniter import croniter

_BACKEND = Path(__file__).resolve().parent.parent
_SCHEDULES_DIR  = _BACKEND / "data" / "schedules"
_STATE_FILE     = _BACKEND / "data" / "jobmaster_state.json"
_LOCAL_LLM_URL  = "http://localhost:8090/v1/models"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _check_local_llm() -> bool:
    import requests
    try:
        r = requests.get(_LOCAL_LLM_URL, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def check_local_llm() -> dict:
    """公开的本地模型健康检查，供 API 端点调用"""
    healthy = _check_local_llm()
    return {"healthy": healthy, "url": _LOCAL_LLM_URL}


def _next_run(cron_expr: str) -> Optional[str]:
    try:
        it = croniter(cron_expr, datetime.utcnow())
        return it.get_next(datetime).isoformat() + "Z"
    except Exception:
        return None


# agent_name → schedule IDs（与 task_bridge.SCHEDULE_AGENT_MAP 保持一致）
_AGENT_SCHEDULE_IDS: Dict[str, List[str]] = {
    "competitor":  ["nightly-exploration"],
    "darwin":      [],
    "reply":       ["nightly-training"],
    "req_analyst": ["weekly-report"],
    "kb_fact":     ["weekly-fact-extraction", "oneshot-backfill-facts"],
    "adopted":          ["weekly-adopted-extract"],
    "handover_suggest": ["weekly-handover-extract"],
}


def get_agent_schedule_info(agent_name: str) -> Dict:
    """返回 agent 对应调度的 next_run / last_run_schedule"""
    sched_ids = _AGENT_SCHEDULE_IDS.get(agent_name, [])
    if not sched_ids:
        return {}
    schedules = [s for s in get_schedules() if s["id"] in sched_ids]
    next_runs = [s["next_run"] for s in schedules if s.get("next_run")]
    last_runs = [s["last_run"] for s in schedules if s.get("last_run")]
    return {
        "next_run": min(next_runs) if next_runs else None,
        "last_run_schedule": max(last_runs) if last_runs else None,
    }


def get_schedules() -> List[Dict]:
    results = []
    if not _SCHEDULES_DIR.exists():
        return results
    for p in sorted(_SCHEDULES_DIR.glob("*.json")):
        data = _read_json(p)
        if not data or not isinstance(data, dict):
            continue
        sid = data.get("id", p.stem)
        cron = data.get("cron", "")
        last = data.get("last_run")
        # 跳过 jobmaster 自身的调度定义
        if sid.startswith("jobmaster-"):
            continue
        host_constraint = data.get("host_constraint")  # None = 双跑
        results.append({
            "id": sid,
            "name": data.get("name", sid),
            "cron": cron,
            "enabled": data.get("enabled", True),
            "task_type": data.get("task_type", "unknown"),
            "last_run": last,
            "next_run": _next_run(cron) if cron else None,
            "status": "running" if _is_running(sid) else ("waiting" if data.get("enabled") else "disabled"),
            "host_constraint": host_constraint,
            "runs_here": host_constraint is None or host_constraint == HOST,
        })
    return results


def _is_running(schedule_id: str) -> bool:
    from services.agent_task_store import AgentTaskStore
    from agents.base import AgentStatus
    store = AgentTaskStore.get_instance()
    tasks = store.list_recent(status=AgentStatus.RUNNING.value, limit=50)
    for t in tasks:
        if t.trigger_src and schedule_id in t.trigger_src:
            return True
    return False


def get_jobmaster_state() -> Dict:
    state = _read_json(_STATE_FILE) or {}
    schedules = get_schedules()
    pending = state.get("pending_decisions", [])
    active_pending = [p for p in pending if isinstance(p, dict)
                      and p.get("status", "") not in ("expired_default:A",
                                                       "expired_default:B",
                                                       "expired_default:C",
                                                       "resolved")]
    return {
        "state_file_updated": state.get("last_run"),
        "schedules": schedules,
        "pending_decisions": len(active_pending),
        "local_llm_healthy": _check_local_llm(),
        "adoption_rate_today": _latest_adoption_rate(state),
    }


def _latest_adoption_rate(state: dict) -> Optional[float]:
    history = state.get("adoption_rate_history", [])
    if history:
        return history[-1].get("rate")
    return None


def get_schedule_summary() -> List[Dict]:
    """每个 schedule 一张卡片：含最近 1 条 task 摘要 + 近 30 天历史计数。"""
    from services.agent_task_store import AgentTaskStore
    store = AgentTaskStore.get_instance()
    # Build reverse map: schedule_id → agent_name
    _sched_to_agent: Dict[str, str] = {}
    for agent_name, sched_ids in _AGENT_SCHEDULE_IDS.items():
        for sid in sched_ids:
            _sched_to_agent[sid] = agent_name
    result = []
    for sched in get_schedules():
        sid = sched["id"]
        recent = store.list_by_schedule(sid, limit=30, days=30)
        latest = recent[0] if recent else None
        # Prefer latest_task.agent_name, fall back to static map
        lt_agent = (getattr(latest, "agent_name", None) if latest else None) or _sched_to_agent.get(sid)
        result.append({
            **sched,
            "agent_name": lt_agent,
            "history_count": len(recent),
            "latest_task": {
                "id": latest.id,
                "status": latest.status.value,
                "title": latest.title,
                "agent_name": getattr(latest, "agent_name", None),
                "started_at": latest.started_at.isoformat() + "Z" if latest.started_at else None,
                "finished_at": latest.finished_at.isoformat() + "Z" if latest.finished_at else None,
                "progress": latest.progress,
            } if latest else None,
        })
    return result


def enqueue_pending_decision(decision_id: str) -> bool:
    """
    将决策放入 JobMaster 编排队列（status=queued）。
    JobMaster 闲时会自动以 default_choice 执行，并在需要时自动启动本地模型。
    """
    import fcntl, os
    lock_path = str(_STATE_FILE) + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            state = _read_json(_STATE_FILE) or {}
            pending = state.get("pending_decisions", [])
            found = False
            for p in pending:
                if isinstance(p, dict) and p.get("id") == decision_id:
                    p["status"] = "queued"
                    p["queued_at"] = datetime.utcnow().isoformat() + "Z"
                    p["queued_default_choice"] = p.get("default", "A")
                    found = True
                    break
            if not found:
                return False
            state["pending_decisions"] = pending
            tmp = _STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(_STATE_FILE))
            # 异步处理队列中的决策（不阻塞请求）
            import threading
            threading.Thread(target=_process_queued_decisions, daemon=True).start()
            return True
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _process_queued_decisions() -> None:
    """
    JobMaster 后台线程：处理 status=queued 的决策。
    1. 检查 task_id 是否需要本地 LLM（含 local 标志的任务）
    2. 若需要且本地模型未运行，自动 kickstart
    3. 等模型就绪后以 default_choice 执行决策
    """
    import time, subprocess, os
    state = _read_json(_STATE_FILE) or {}
    queued = [p for p in state.get("pending_decisions", [])
              if isinstance(p, dict) and p.get("status") == "queued"]
    if not queued:
        return

    # 判断是否有需要本地 LLM 的任务
    local_llm_task_ids = {"nightly-exploration", "darwin"}
    needs_local = any(q.get("task_id", "") in local_llm_task_ids for q in queued)

    if needs_local:
        llm_ok = check_local_llm().get("healthy", False)
        if not llm_ok:
            try:
                plist = os.path.expanduser(
                    "~/Library/LaunchAgents/com.aiticket.supergemma4.plist"
                )
                if os.path.exists(plist):
                    uid = os.getuid()
                    target = f"gui/{uid}/com.aiticket.supergemma4"
                    subprocess.run(["launchctl", "kickstart", "-k", target],
                                   capture_output=True, timeout=5)
            except Exception:
                pass
            # 最多等 90s 等模型就绪
            for _ in range(9):
                time.sleep(10)
                if check_local_llm().get("healthy", False):
                    break

    # 以 default_choice 逐个 resolve
    for q in queued:
        resolve_pending_decision(
            q["id"],
            q.get("queued_default_choice", q.get("default", "A")),
            note="JobMaster 编排队列自动执行",
        )


def get_pending_decisions() -> List[Dict]:
    """返回未处理的待决策列表（详情）。"""
    state = _read_json(_STATE_FILE) or {}
    pending = state.get("pending_decisions", [])
    terminal = {"expired_default:A", "expired_default:B", "expired_default:C", "resolved", "queued"}
    return [p for p in pending if isinstance(p, dict) and p.get("status", "") not in terminal]


def resolve_pending_decision(decision_id: str, choice: str, note: str = "") -> bool:
    """
    将 pending_decisions[id] 标记为 resolved。
    返回 True=找到并写入，False=未找到。
    原子写：读→改→rename。
    """
    import fcntl, tempfile, os
    lock_path = str(_STATE_FILE) + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            state = _read_json(_STATE_FILE) or {}
            pending = state.get("pending_decisions", [])
            found = False
            for p in pending:
                if isinstance(p, dict) and p.get("id") == decision_id:
                    p["status"] = "resolved"
                    p["resolved_choice"] = choice
                    p["resolved_note"] = note
                    p["resolved_at"] = datetime.utcnow().isoformat() + "Z"
                    found = True
                    break
            if not found:
                return False
            state["pending_decisions"] = pending
            tmp = _STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(_STATE_FILE))
            return True
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
