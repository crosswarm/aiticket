"""CompetitorAgent — 竞品调研 Agent，包装 scripts/exploration_agent.py。"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from agents.base import AgentTask, AgentStatus, BaseAgent
from agents.self_monitor_mixin import AgentSelfMonitorMixin

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "exploration_agent.py"

# exploration_agent.py 中定义的任务 id → 展示名
_TARGETS = [
    ("bip-designer-nodes",         "BIP 流程节点探索"),
    ("bip-designer-conditions",    "BIP 流程条件探索"),
    ("kingdee-designer-nodes",     "金蝶流程节点探索"),
    ("kingdee-approval-opinions",  "金蝶审批意见探索"),
]


class CompetitorAgent(AgentSelfMonitorMixin, BaseAgent):
    expected_run_interval_hours: float = 24
    name         = "competitor"
    display_name = "竞品调研 Agent"
    description  = "夜间自动探索 BIP+金蝶工作流；网站截图+功能分析+洞察报告"
    version      = "1.0"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.list_capabilities(),
        }

    def list_capabilities(self) -> List[str]:
        caps = ["bip-exploration", "kingdee-exploration", "opencli", "screenshot", "llm-analysis"]
        if not _SCRIPT.exists():
            caps.append("script-missing")
        return caps

    def health_check(self) -> dict:
        if not _SCRIPT.exists():
            return {"healthy": False, "detail": f"exploration_agent.py not found"}
        return {"healthy": True, "detail": "script ok"}

    def run_task(self, task: AgentTask) -> dict:
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()
        completed = 0
        failed = 0

        for i, (target_id, target_name) in enumerate(_TARGETS):
            sub = AgentTask.new(
                agent_name="competitor",
                title=target_name,
                trigger_src=f"agent:competitor:{target_id}",
                parent_id=task.id,
            )
            sub.status = AgentStatus.RUNNING
            sub.started_at = datetime.utcnow()
            store.insert(sub)
            self.report_progress(
                task.id,
                int(i / len(_TARGETS) * 100),
                f"正在探索: {target_name}",
            )

            try:
                result = self._run_target(sub, target_id, store)
                store.update_status(
                    sub.id, AgentStatus.SUCCEEDED,
                    finished_at=datetime.utcnow(),
                    result_json=json.dumps(result, ensure_ascii=False, default=str),
                    progress=100,
                )
                completed += 1
            except Exception as e:
                store.update_status(sub.id, AgentStatus.FAILED, finished_at=datetime.utcnow())
                store.append_log(sub.id, f"ERROR: {e}")
                failed += 1

        return {"targets": len(_TARGETS), "completed": completed, "failed": failed}

    def _run_target(self, task: AgentTask, target_id: str, store) -> dict:
        store.append_log(task.id, f"启动探索任务: {target_id}")
        if not _SCRIPT.exists():
            store.append_log(task.id, "exploration_agent.py 未找到，跳过")
            return {"status": "skipped", "reason": "script_missing"}

        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--task", target_id],
            capture_output=True, text=True, timeout=300,
            cwd=str(_SCRIPT.parent.parent.parent),
        )
        output = (proc.stdout or "")[-1000:] + (proc.stderr or "")[-500:]
        store.append_log(task.id, output or "(no output)")
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}")
        return {"status": "completed", "target": target_id}
