"""KbFactAgent — KB事实抽取 Agent 适配器，包装 scripts/extract_facts_from_kb.py。"""
from __future__ import annotations
from pathlib import Path
from typing import List
from agents.base import BaseAgent

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "extract_facts_from_kb.py"


class KbFactAgent(BaseAgent):
    name         = "kb_fact"
    display_name = "KB 事实抽取 Agent"
    description  = "124篇KB文档→产品事实条目，SHA1去重；SuperGemma4本地推理，零外部API成本"
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
        return ["kb-extraction", "fact-dedup", "local-llm", "batch-processing"]

    def health_check(self) -> dict:
        if not _SCRIPT.exists():
            return {"healthy": False, "detail": f"script not found: {_SCRIPT.name}"}
        local_ok = self._check_local_llm()
        if not local_ok:
            return {"healthy": False, "detail": "SuperGemma4 (localhost:8090) unreachable"}
        return {"healthy": True, "detail": "script ok, local LLM reachable"}

    def _check_local_llm(self) -> bool:
        try:
            import sys
            _b = str(Path(__file__).resolve().parent.parent)
            if _b not in sys.path:
                sys.path.insert(0, _b)
            from services.local_llm_lifecycle import ensure_running
            return ensure_running()
        except Exception:
            import requests
            try:
                r = requests.get("http://localhost:8090/v1/models", timeout=3)
                return r.status_code == 200
            except Exception:
                return False
