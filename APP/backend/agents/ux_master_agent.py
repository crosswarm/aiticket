"""UXMasterAgent — DEPRECATED 2026-05-02，已合并入 UXDeverAgent (Muse)。
保留此文件仅为向后兼容导入；不再有独立 identity yaml。"""
from __future__ import annotations

import logging
from typing import List, Optional

from agents.base import AgentTask, BaseAgent
from agents.parent_mixin import ParentAgentMixin
from agents.ux_dever_agent import UXDeverAgent  # noqa: F401 — deprecation alias target

logger = logging.getLogger(__name__)


class UXMasterAgent(ParentAgentMixin, BaseAgent):
    name         = "ux_master"
    display_name = "UX设计主导 Agent"
    description  = "主导 UX 设计策略，确保跨需求的体验一致性与可用性标准"
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
        return ["ux-strategy", "design-consistency", "usability-review", "ux-decision"]

    def health_check(self) -> dict:
        return {"healthy": True, "detail": "ok"}

    def run_task(self, task: AgentTask) -> Optional[dict]:
        logger.info(f"[UXMasterAgent] task={task.id}")
        return {"status": "stub", "message": "UXMasterAgent run_task not yet implemented"}


# Deprecation alias — 2026-05-02 Aria 合并入 Muse (UXDeverAgent)
UXMasterAgent = UXDeverAgent  # type: ignore[assignment]
