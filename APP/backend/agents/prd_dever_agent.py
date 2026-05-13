"""PRDDeverAgent — DEPRECATED 2026-05-02，已合并入 ReqAnalystAgent (Atlas)。
保留此文件仅为向后兼容导入；不再有独立 identity yaml。"""
from __future__ import annotations

import logging
from typing import List, Optional

from agents.base import AgentTask, BaseAgent
from agents.req_analyst_agent import ReqAnalystAgent  # noqa: F401 — deprecation alias target

logger = logging.getLogger(__name__)


class PRDDeverAgent(BaseAgent):
    name         = "prd_dever"
    display_name = "PRD产品分析 Agent"
    description  = "从工单中提炼产品需求洞察，输出结构化分析供 PRDMaster 决策"
    version      = "1.0"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "kind": "internal_dever",
            "capabilities": self.list_capabilities(),
        }

    def list_capabilities(self) -> List[str]:
        return ["requirement-analysis", "gap-detection", "user-story-extraction"]

    def health_check(self) -> dict:
        return {"healthy": True, "detail": "ok"}

    def run_task(self, task: AgentTask) -> Optional[dict]:
        logger.info(f"[PRDDeverAgent] task={task.id}")
        return {"status": "stub", "message": "PRDDeverAgent run_task not yet implemented"}


# Deprecation alias — 2026-05-02 Rex 合并入 Atlas (ReqAnalystAgent)
PRDDeverAgent = ReqAnalystAgent  # type: ignore[assignment]
