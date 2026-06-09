"""AgentSelfMonitorMixin — schedule-driven agents 自查上次运行时间"""
from __future__ import annotations
from datetime import datetime
from typing import ClassVar, Optional


class AgentSelfMonitorMixin:
    """
    继承此 mixin 的 agent 必须声明 expected_run_interval_hours。
    None = adhoc agent，不参与自监督检查。
    """

    expected_run_interval_hours: ClassVar[Optional[float]] = None
    self_monitor_grace_factor: ClassVar[float] = 2.0

    def self_check_last_run(self) -> dict:
        """返回 {'status': ..., 'age_hours': ..., 'alert': bool}"""
        if self.expected_run_interval_hours is None:
            return {"status": "adhoc", "skipped": True, "alert": False}

        try:
            from services.agent_task_store import AgentTaskStore
            store = AgentTaskStore.get_instance()
            recent = store.recent_succeeded(getattr(self, "name", ""), n=1)
        except Exception:
            return {"status": "unknown", "alert": False}

        if not recent:
            # 区分"刚接 mixin / notify_trigger 失败"与"真的从未跑过"
            grace = self._schedule_phase_in_check()
            if grace["in_grace"]:
                return {"status": "phase_in", "alert": False, "msg": grace["msg"]}
            return {"status": "never_run", "alert": True,
                    "msg": f"{getattr(self, 'name', '?')} 从未成功执行过"}

        last_finished = recent[0].get("finished_at") or recent[0].get("created_at")
        if not last_finished:
            return {"status": "unknown", "alert": False}

        try:
            if isinstance(last_finished, str):
                last_dt = datetime.fromisoformat(last_finished.rstrip("Z"))
            else:
                last_dt = last_finished
        except ValueError:
            return {"status": "unknown", "alert": False}

        age_h = (datetime.utcnow() - last_dt).total_seconds() / 3600
        max_h = self.expected_run_interval_hours * self.self_monitor_grace_factor

        if age_h > max_h:
            return {
                "status": "overdue",
                "age_hours": round(age_h, 1),
                "max_hours": round(max_h, 1),
                "alert": True,
                "msg": f"{getattr(self, 'name', '?')} 已 {round(age_h,1)}h 未成功执行（期望≤{round(max_h,1)}h）",
            }

        return {"status": "healthy", "age_hours": round(age_h, 1), "alert": False}

    def _schedule_phase_in_check(self) -> dict:
        """反查对应 schedule JSON 的 last_run / run_count。
        若 schedule 有执行记录但 agent_tasks 没有，说明 notify_trigger 之前失败过（B1 范畴），
        此时返回 in_grace=True 避免误报；B2 handler 会用双证模式单独告警。"""
        try:
            from services.task_bridge import SCHEDULE_AGENT_MAP
            from pathlib import Path
            import json as _json
            name = getattr(self, "name", "")
            sids = [sid for sid, a in SCHEDULE_AGENT_MAP.items() if a == name]
            if not sids:
                return {"in_grace": False, "msg": "无对应 schedule"}
            sdir = Path(__file__).resolve().parent.parent / "data" / "schedules"
            for sid in sids:
                f = sdir / f"{sid}.json"
                if not f.exists():
                    continue
                d = _json.loads(f.read_text(encoding="utf-8"))
                if not d.get("enabled", True):
                    continue
                if d.get("run_count", 0) > 0 or d.get("last_run"):
                    return {
                        "in_grace": True,
                        "msg": (
                            f"{name} schedule={sid} 已跑过（JSON run_count={d.get('run_count',0)}）"
                            f"但 agent_tasks 无记录——可能 notify_trigger 历史失败，见 scheduler 日志"
                        ),
                    }
            return {"in_grace": False, "msg": ""}
        except Exception:
            return {"in_grace": False, "msg": ""}
