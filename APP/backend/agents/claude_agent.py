"""
ClaudeAgent — 把 Claude Code 原生 bash run_in_background 任务
作为一个虚拟 Agent 暴露在 /api/agents 列表中。

设计：方案 A — 文件系统扫描器把任务写入 agent_tasks，
ClaudeAgent 仅作为 Registry 中的"门面"，让 agents.html
零改动地展示这些任务。

详见：conclusion/temp/AGENT-CLAUDE-NATIVE-V1.md
"""
from __future__ import annotations

from typing import List

from agents.base import BaseAgent
from agents.parent_mixin import ParentAgentMixin


class ClaudeAgent(ParentAgentMixin, BaseAgent):
    """Claude Code 原生任务代理 Agent。

    这个 Agent 不主动跑业务逻辑，它的存在只是为了让
    Claude Code 的 bash run_in_background 任务（视频渲染、
    批处理脚本等）以"agent_name=claude"的形态在
    agents.html 中可见。

    任务写入 agent_tasks 表的工作由 ClaudeTaskScanner 完成。
    """

    name = "claude"
    display_name = "Claude 原生任务"
    description = "Claude Code 在本机发起的 bash run_in_background 任务（视频渲染 / 批处理 / 长跑脚本）"
    version = "1.0"
    hidden = False
    tags = ["原生", "外部", "桥接"]

    def __init__(self, scanner=None):
        super().__init__()
        self._scanner = scanner

    def describe(self) -> dict:
        scanner_healthy = self._scanner.healthy() if self._scanner else False
        scanner_dir = str(self._scanner.project_dir) if self._scanner and self._scanner.project_dir else "(unavailable)"
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.list_capabilities(),
            "scanner": {
                "healthy": scanner_healthy,
                "task_dir": scanner_dir,
            },
        }

    def list_capabilities(self) -> List[str]:
        return ["native-bash-monitor", "filesystem-scanner", "progress-inference"]

    def health_check(self) -> dict:
        if self._scanner is None:
            return {"healthy": False, "detail": "scanner not attached"}
        if not self._scanner.healthy():
            return {
                "healthy": False,
                "detail": f"task dir not found: expected /tmp/claude-501/<workspace>",
            }
        return {"healthy": True, "detail": f"scanning {self._scanner.project_dir}"}

    def run_task(self, task):
        """ClaudeAgent 不支持手动触发 — 任务由 Claude Code CLI 自身发起。"""
        raise NotImplementedError(
            "Claude 原生任务由 Claude Code 自身发起，无法从 JobMaster 端手动触发"
        )
