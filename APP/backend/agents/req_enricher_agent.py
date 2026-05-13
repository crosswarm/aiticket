"""
req_enricher_agent.py — 需求 cluster 事实加工 Agent

接收 cluster_id，并行启动 4 个子任务：
  1. bip_kb_sub     — BIP 现状（kb_compiled）
  2. module_cap_sub — 关联模块能力（kb_local）
  3. ticket_corr_sub — 扩大召回相关工单
  4. competitor_sub  — 竞品数据（带缓存，cache-first）

合并结果写入 req_clusters.metadata.requirement_fact_packet
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND = Path(__file__).resolve().parent.parent
import sys
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.base import AgentStatus, AgentTask, BaseAgent


class ReqEnricherAgent(BaseAgent):
    name = "req_enricher"
    display_name = "需求事实加工 Agent"
    capabilities = ["kb-search", "competitor-research", "ticket-correlation"]

    def __init__(self, vector_store=None):
        super().__init__()
        self._vs = vector_store

    def _vs_instance(self):
        if self._vs is not None:
            return self._vs
        from vector_store import VectorStore
        return VectorStore.get_instance()

    def describe(self) -> dict:
        return {"name": self.name, "display_name": self.display_name, "capabilities": self.list_capabilities()}

    def list_capabilities(self):
        return ["bip_kb", "module_cap", "ticket_corr", "competitor"]

    # ── 主任务入口 ───────────────────────────────────────────────────
    def run_task(self, task: AgentTask) -> Optional[dict]:
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()

        payload = json.loads(task.payload_json or "{}")
        cluster_id = payload.get("cluster_id")
        if not cluster_id:
            self.append_log(task.id, "❌ payload 缺少 cluster_id")
            return {"error": "missing cluster_id"}

        vs = self._vs_instance()
        cluster = vs.get_cluster(cluster_id)
        if not cluster:
            self.append_log(task.id, f"❌ 找不到 cluster: {cluster_id}")
            return {"error": f"cluster not found: {cluster_id}"}

        topic = cluster.get("topic_l2") or cluster.get("topic_l1") or "未知主题"
        scenario = cluster.get("core_scenario") or topic
        member_ids = json.loads(cluster.get("member_req_ids", "[]"))

        self.append_log(task.id, f"🔬 开始事实加工：{topic}（{len(member_ids)} 条需求）")
        self.report_progress(task.id, 5)

        results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        sub_defs = [
            ("bip_kb_sub",     "BIP 现状检索",   self._run_bip_kb),
            ("module_cap_sub", "模块能力检索",   self._run_module_cap),
            ("ticket_corr_sub","相关工单召回",   self._run_ticket_corr),
            ("competitor_sub", "竞品数据检索",   self._run_competitor),
        ]

        # 创建子任务记录
        subs = {}
        for key, title, _ in sub_defs:
            subs[key] = self._create_sub(store, task.id, title)

        # 并行执行
        def _worker(key: str, title: str, fn, sub: AgentTask):
            try:
                data = fn(sub, store, vs, topic, scenario, member_ids, cluster)
                results[key] = data
                self._finish_sub(store, sub.id, {"count": len(data) if isinstance(data, list) else 1})
                self.append_log(task.id, f"✅ {title} 完成，获得 {len(data) if isinstance(data, list) else 1} 条")
            except Exception as exc:
                errors[key] = str(exc)
                self._fail_sub(store, sub.id, exc)
                self.append_log(task.id, f"⚠️ {title} 失败（{exc}），部分加工继续")

        threads = []
        for key, title, fn in sub_defs:
            t = threading.Thread(
                target=_worker, args=(key, title, fn, subs[key]),
                name=f"enrich-{cluster_id[:8]}-{key}", daemon=True,
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=180)

        self.report_progress(task.id, 85)

        # 组装 fact_packet
        fact_packet = {
            "bip_current_state":   results.get("bip_kb_sub", []),
            "related_modules":     results.get("module_cap_sub", []),
            "related_tickets":     results.get("ticket_corr_sub", []),
            "competitor_evidence": results.get("competitor_sub", []),
            "enrich_ts": datetime.utcnow().isoformat(),
            "partial": bool(errors),
        }
        vs.update_cluster_field(cluster_id, {
            "requirement_fact_packet": json.dumps(fact_packet, ensure_ascii=False, default=str),
            "fact_packet_ready": not bool(errors),
            "updated_at": datetime.utcnow().isoformat(),
        })

        self.report_progress(task.id, 100)
        msg = f"🔬 {topic} 事实加工完成（{'部分失败: ' + ', '.join(errors) if errors else '全量'}）"
        self.append_log(task.id, msg)
        return {"cluster_id": cluster_id, "partial": bool(errors), "errors": errors}

    # ── 子任务实现 ───────────────────────────────────────────────────
    def _run_bip_kb(self, sub, store, vs, topic, scenario, member_ids, cluster) -> list:
        from kb_runtime_service import KnowledgeRuntimeService
        svc = KnowledgeRuntimeService()
        hits = svc.search(scenario, source_kind="kb_compiled", top_k=15)
        return [
            {"kb_id": h.get("id", ""), "summary": h.get("content", "")[:300], "citation": h.get("source", "")}
            for h in (hits or [])
        ]

    def _run_module_cap(self, sub, store, vs, topic, scenario, member_ids, cluster) -> list:
        from kb_runtime_service import KnowledgeRuntimeService
        svc = KnowledgeRuntimeService()
        hits = svc.search(scenario, source_kind="kb_local", top_k=20)
        return [
            {"kb_id": h.get("id", ""), "summary": h.get("content", "")[:300], "citation": h.get("source", "")}
            for h in (hits or [])
        ]

    def _run_ticket_corr(self, sub, store, vs, topic, scenario, member_ids, cluster) -> list:
        hits = vs.search_similar_issues(scenario, top_k=50)
        seen = set(member_ids)
        results = []
        for h in (hits or []):
            key = h.get("issue_key") or h.get("id", "")
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "issue_key": key,
                "summary": h.get("summary", h.get("content", ""))[:200],
                "similarity": round(float(h.get("score", 0)), 4),
            })
        return results[:30]

    def _run_competitor(self, sub, store, vs, topic, scenario, member_ids, cluster) -> list:
        from competitor_research_service import CompetitorResearchService
        svc = CompetitorResearchService()
        requirement = {"title": topic, "description": scenario}
        evidence = svc.research_with_cache(requirement)
        results = []
        for e in (evidence or []):
            results.append({
                "vendor": e.get("vendor", ""),
                "feature": e.get("feature", ""),
                "support_status": e.get("support_status", ""),
                "ui_url": e.get("ui_url", "") or e.get("screenshot_url", ""),
                "citation": e.get("source", "") or e.get("citation", ""),
                "freshness": e.get("freshness", "unknown"),
            })
        return results

    # ── 工具方法 ────────────────────────────────────────────────────
    def _create_sub(self, store, parent_id: str, title: str) -> AgentTask:
        from services.agent_task_store import AgentTaskStore
        sub = AgentTask.new(
            agent_name=self.name,
            title=title,
            trigger_src=f"agent:{self.name}",
            parent_id=parent_id,
        )
        sub.status = AgentStatus.RUNNING
        sub.started_at = datetime.utcnow()
        AgentTaskStore.get_instance().insert(sub)
        return sub

    def _finish_sub(self, store, sub_id: str, result: dict) -> None:
        from services.agent_task_store import AgentTaskStore
        AgentTaskStore.get_instance().update_status(
            sub_id, AgentStatus.SUCCEEDED,
            finished_at=datetime.utcnow(),
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            progress=100,
        )

    def _fail_sub(self, store, sub_id: str, exc: Exception) -> None:
        from services.agent_task_store import AgentTaskStore
        AgentTaskStore.get_instance().update_status(
            sub_id, AgentStatus.FAILED,
            finished_at=datetime.utcnow(),
            result_json=json.dumps({"error": str(exc)}, ensure_ascii=False),
        )
