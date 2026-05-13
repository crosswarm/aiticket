"""PRDMasterAgent — PRD 产品主导 Agent（需求方案决策 + OMC planner/analyst 授权）"""
from __future__ import annotations

import logging
from typing import List, Optional

from agents.base import AgentTask, BaseAgent
from agents.parent_mixin import ParentAgentMixin

logger = logging.getLogger(__name__)


class PRDMasterAgent(ParentAgentMixin, BaseAgent):
    name         = "prd_master"
    display_name = "PRD产品主导 Agent"
    description  = "主导产品需求决策，授权 OMC planner/analyst/critic/verifier 类 subagent"
    version      = "1.0"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "kind": "internal_master",
            "capabilities": self.list_capabilities(),
            "managed_subagents": self.list_managed_subagents(),
        }

    def list_capabilities(self) -> List[str]:
        return ["prd-strategy", "requirement-synthesis", "feature-prioritization", "subagent-orchestration"]

    def health_check(self) -> dict:
        return {"healthy": True, "detail": "ok"}

    def run_task(self, task: AgentTask) -> Optional[dict]:
        logger.info(f"[PRDMasterAgent] task={task.id}")
        return {"status": "stub", "message": "PRDMasterAgent run_task not yet implemented"}
