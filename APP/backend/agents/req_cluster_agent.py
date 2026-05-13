"""
req_cluster_agent.py — 需求池周度聚类 Agent

三阶段串行执行（每阶段创建子任务卡片以便在 agents.html 中实时可见）：
  1. clusterer  — 按 topic_l2/l1 自适应分组，写入 req_clusters collection
  2. valuer     — 量化 + LLM 评分，过滤低价值 cluster
  3. convergor  — 对高价值 cluster 调 req_analyst 生成 core_scenario/problem_statement
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND = Path(__file__).resolve().parent.parent
import sys
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.base import AgentStatus, AgentTask, BaseAgent


PRIORITY_WEIGHT = {"紧急": 4, "高": 3, "中": 2, "低": 1}
VALUE_THRESHOLD = 0.3  # 低于此分数标 low_value，但保留
MIN_L2_SIZE = 2        # L2 cluster 成员数 < 此值则合并回 L1


class ReqClusterAgent(BaseAgent):
    name = "req_cluster"
    display_name = "需求池周度聚类 Agent"
    capabilities = ["cluster-requirements", "value-scoring", "scenario-convergence"]

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
        return ["cluster", "value_score", "dedup"]

    def _load_topic_names(self) -> dict:
        """Parse topic.md → {dotted_code_without_TOP: Chinese name}. Delegates to topic_codec."""
        try:
            from services.topic_codec import load_topic_names
            return load_topic_names()
        except Exception:
            import re
            topic_md = _BACKEND / "data" / "topic.md"
            code2name: dict = {}
            if not topic_md.exists():
                return code2name
            with open(topic_md, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"^\s*-\s*\[(TOP-([A-Z._]+))\]\s*(\S.*)", line)
                    if m:
                        code2name[m.group(2)] = m.group(3).strip()
            return code2name

    # ── 主任务入口 ───────────────────────────────────────────────────
    def run_task(self, task: AgentTask) -> Optional[dict]:
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()

        self.append_log(task.id, "🗂️ 需求池周度聚类开始")
        self.report_progress(task.id, 5)

        # 读取用户主题引导（可选）
        theme_hints: list = []
        try:
            payload = json.loads(task.payload_json or "{}")
            theme_hints = payload.get("theme_hints") or []
            if theme_hints:
                self.append_log(task.id, f"💡 主题方向引导：{theme_hints}")
        except Exception:
            pass

        vs = self._vs_instance()

        # ── Phase 1: 聚类 ──────────────────────────────────────────
        cluster_sub = self._create_sub(store, task.id, "clusterer", "需求分组聚类")
        try:
            clusters = self._run_clusterer(cluster_sub, store, vs)
            self._finish_sub(store, cluster_sub.id, {"clusters": len(clusters)})
        except Exception as exc:
            self._fail_sub(store, cluster_sub.id, exc)
            self.append_log(task.id, f"❌ 聚类阶段失败: {exc}")
            return {"error": str(exc), "phase": "clusterer"}

        self.report_progress(task.id, 35)
        self.append_log(task.id, f"✅ 聚类完成：共 {len(clusters)} 个 cluster")

        # ── Phase 2: 价值评分 ──────────────────────────────────────
        valuer_sub = self._create_sub(store, task.id, "valuer", "价值评分与过滤")
        try:
            clusters = self._run_valuer(valuer_sub, store, vs, clusters, theme_hints=theme_hints)
            high = sum(1 for c in clusters if c.get("value_score", 0) >= VALUE_THRESHOLD)
            low = len(clusters) - high
            self._finish_sub(store, valuer_sub.id, {"high_value": high, "low_value": low})
        except Exception as exc:
            self._fail_sub(store, valuer_sub.id, exc)
            self.append_log(task.id, f"❌ 评分阶段失败: {exc}")

        self.report_progress(task.id, 65)
        high_value_clusters = [c for c in clusters if c.get("value_score", 0) >= VALUE_THRESHOLD]
        low_value_count = len(clusters) - len(high_value_clusters)
        self.append_log(
            task.id,
            f"📊 评分完成：高价值 {len(high_value_clusters)} 个，低价值 {low_value_count} 个（保留但标记）"
        )

        # ── Phase 3: 场景收敛 ──────────────────────────────────────
        convergor_sub = self._create_sub(store, task.id, "convergor", "场景描述收敛")
        try:
            converged = self._run_convergor(convergor_sub, store, vs, high_value_clusters)
            self._finish_sub(store, convergor_sub.id, {"converged": converged})
        except Exception as exc:
            self._fail_sub(store, convergor_sub.id, exc)
            self.append_log(task.id, f"❌ 收敛阶段失败: {exc}")
            converged = 0

        self.report_progress(task.id, 95)
        summary = (
            f"🎯 周度聚类完成\n"
            f"共 {len(clusters)} 个 cluster（高价值 {len(high_value_clusters)} 个，低价值 {low_value_count} 个）\n"
            f"场景收敛 {converged} 个，进入事实加工队列"
        )
        self.append_log(task.id, summary)

        try:
            from feishu_notifier import get_notifier
            get_notifier().send_message(summary)
        except Exception:
            pass

        # 写 reqpool_metrics.jsonl 供 JobMaster 度量
        try:
            from pathlib import Path as _P
            metrics_path = _P(__file__).resolve().parents[3] / "conclusion" / "reqpool_metrics.jsonl"
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            approved_count = sum(1 for c in clusters if c.get("pm_approved"))
            rejected_count = sum(
                1 for c in vs.list_clusters(status="rejected")
            )
            with open(metrics_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "week": datetime.utcnow().strftime("%Y-W%W"),
                    "clusters_new": len(clusters),
                    "clusters_low": low_value_count,
                    "solutions_generated": converged,
                    "pm_approved": approved_count,
                    "pm_rejected": rejected_count,
                    "pm_adoption_rate": round(approved_count / max(len(high_value_clusters), 1) * 100, 1),
                    "ts": datetime.utcnow().isoformat(),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

        return {
            "clusters_total": len(clusters),
            "high_value": len(high_value_clusters),
            "low_value": low_value_count,
            "converged": converged,
        }

    # ── Phase 1: Clusterer ──────────────────────────────────────────
    def _run_clusterer(self, sub: AgentTask, store, vs) -> List[dict]:
        from services.agent_task_store import AgentTaskStore
        self.append_log(sub.id, "读取 req_pool 所有需求...")

        all_reqs = self._load_all_requirements(vs)
        self.append_log(sub.id, f"共读取 {len(all_reqs)} 条需求，开始按主题分组")

        week_iso = datetime.utcnow().strftime("%Y-W%W")

        # 按 topic_l2 分组（空 topic_l2 的归到 topic_l1）
        l2_groups: Dict[str, list] = {}
        for req in all_reqs:
            l1 = req.get("topic_l1") or "未分类"
            l2 = req.get("topic_l2") or ""
            key = f"{l1}::{l2}" if l2 else f"{l1}::"
            l2_groups.setdefault(key, []).append(req)

        # 自适应：L2 < MIN_L2_SIZE 则合并回 L1
        l1_groups: Dict[str, list] = {}
        final_clusters: List[dict] = []

        for key, members in l2_groups.items():
            l1, l2 = key.split("::", 1)
            if l2 and len(members) < MIN_L2_SIZE:
                l1_groups.setdefault(l1, []).extend(members)
            else:
                cluster_id = self._stable_cluster_id(l1, l2 or None, week_iso)
                final_clusters.append({
                    "cluster_id": cluster_id,
                    "topic_l1": l1,
                    "topic_l2": l2 or "",
                    "level_used": "l2" if l2 else "l1",
                    "members": members,
                    "week_iso": week_iso,
                })

        for l1, members in l1_groups.items():
            cluster_id = self._stable_cluster_id(l1, None, week_iso)
            final_clusters.append({
                "cluster_id": cluster_id,
                "topic_l1": l1,
                "topic_l2": "",
                "level_used": "l1",
                "members": members,
                "week_iso": week_iso,
            })

        # 写入 req_clusters collection
        now_iso = datetime.utcnow().isoformat()
        code2name = self._load_topic_names()
        for c in final_clusters:
            member_ids = [m.get("req_id", "") for m in c["members"]]
            meta = {
                "cluster_id": c["cluster_id"],
                "week_iso": c["week_iso"],
                "topic_l1": c["topic_l1"],
                "topic_l2": c["topic_l2"],
                "level_used": c["level_used"],
                "seed_source": "auto",
                "member_req_ids": json.dumps(member_ids, ensure_ascii=False),
                "evidence_issue_keys": json.dumps([], ensure_ascii=False),
                "ticket_count": len(member_ids),
                "unique_customer_count": 0,
                "value_score": 0.0,
                "commonality_score": 0.0,
                "necessity_score": 0.0,
                "status": "new",
                "core_scenario": "",
                "problem_statement": "",
                "focused_goal": "",
                "requirement_fact_packet": json.dumps({}, ensure_ascii=False),
                "solutions": json.dumps([], ensure_ascii=False),
                "fact_packet_ready": False,
                "solutions_ready": False,
                "pm_approved": False,
                "reject_reason": "",
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            full_code = (c["topic_l1"] + "." + c["topic_l2"]) if c["topic_l2"] else c["topic_l1"]
            topic_name = code2name.get(full_code) or c["topic_l2"] or c["topic_l1"]
            vs.upsert_cluster(c["cluster_id"], topic_name, meta)

        self.append_log(sub.id, f"写入 {len(final_clusters)} 个 cluster 到 req_clusters collection")
        return final_clusters

    # ── Phase 2: Valuer ─────────────────────────────────────────────
    def _run_valuer(self, sub: AgentTask, store, vs, clusters: List[dict], theme_hints: list = None) -> List[dict]:
        from llm_service import llm_service
        self.append_log(sub.id, f"对 {len(clusters)} 个 cluster 进行价值评分")

        now_utc = datetime.utcnow()
        threshold_30d = (now_utc - timedelta(days=30)).isoformat()

        for i, c in enumerate(clusters):
            members = c.get("members", [])
            ticket_count = len(members)

            # 量化维度
            unique_customers = len({m.get("jira_customer", "") for m in members if m.get("jira_customer")})
            priority_sum = sum(PRIORITY_WEIGHT.get(m.get("jira_priority", "低"), 1) for m in members)
            recent_count = sum(
                1 for m in members
                if (m.get("ingest_ts") or m.get("created_at") or "") >= threshold_30d
            )

            # LLM 维度（GLM）
            titles_text = "\n".join(f"- {m.get('title', '')}" for m in members[:20])
            topic_str = c['topic_l2'] or c['topic_l1']
            # 检查是否命中用户主题引导
            hint_note = ""
            if theme_hints:
                matched = [h for h in theme_hints if any(kw in topic_str for kw in h.split())]
                if matched:
                    hint_note = f"\n注意：用户特别关注此主题（引导词：{'、'.join(matched)}），请适当提升必要性评估。"
            try:
                llm_resp = llm_service.call_llm(
                    f"以下是一组需求工单标题，主题：{topic_str}\n"
                    f"{titles_text}\n\n"
                    f"请评估：\n"
                    f"1. 通用性得分（0.0-1.0）：此需求跨客户普遍程度\n"
                    f"2. 必要性得分（0.0-1.0）：用户痛点紧迫程度{hint_note}\n"
                    f"仅返回JSON：{{\"commonality\": 0.X, \"necessity\": 0.X}}",
                    provider="zhipu",
                    max_tokens=100,
                )
                scores = json.loads(llm_resp.strip().lstrip("```json").rstrip("```").strip())
                commonality = float(scores.get("commonality", 0.5))
                necessity = float(scores.get("necessity", 0.5))
            except Exception:
                commonality = 0.5
                necessity = 0.5

            # 综合分（归一化加权）
            tc_score = min(ticket_count / 20.0, 1.0)
            uc_score = min(unique_customers / 10.0, 1.0)
            pr_score = min(priority_sum / (ticket_count * 4), 1.0) if ticket_count else 0
            rc_score = min(recent_count / max(ticket_count, 1), 1.0)
            value_score = round(
                0.25 * tc_score + 0.20 * uc_score + 0.15 * pr_score +
                0.15 * rc_score + 0.15 * commonality + 0.10 * necessity,
                4
            )

            status = "active" if value_score >= VALUE_THRESHOLD else "low_value"
            c["value_score"] = value_score
            c["commonality_score"] = commonality
            c["necessity_score"] = necessity
            c["unique_customer_count"] = unique_customers
            c["status"] = status

            vs.update_cluster_field(c["cluster_id"], {
                "unique_customer_count": unique_customers,
                "value_score": value_score,
                "commonality_score": commonality,
                "necessity_score": necessity,
                "status": status,
                "updated_at": datetime.utcnow().isoformat(),
            })
            self.append_log(sub.id, f"  [{i+1}/{len(clusters)}] {c['topic_l2'] or c['topic_l1']}: score={value_score:.3f} → {status}")

        return clusters

    # ── Phase 3: Convergor ──────────────────────────────────────────
    def _run_convergor(self, sub: AgentTask, store, vs, clusters: List[dict]) -> int:
        from llm_service import llm_service
        converged = 0
        for i, c in enumerate(clusters):
            members = c.get("members", [])
            if not members:
                continue
            topic = c["topic_l2"] or c["topic_l1"]
            titles_text = "\n".join(f"- {m.get('title', '')} ({m.get('jira_priority', '')})" for m in members[:30])
            try:
                resp = llm_service.call_llm(
                    f"需求主题：{topic}（共 {len(members)} 条工单）\n"
                    f"工单列表：\n{titles_text}\n\n"
                    f"请生成：\n"
                    f"1. core_scenario（核心使用场景，1-2句话）\n"
                    f"2. problem_statement（核心问题陈述，1句话）\n"
                    f"3. focused_goal（聚焦目标，1句话）\n"
                    f"仅返回JSON：{{\"core_scenario\":\"...\",\"problem_statement\":\"...\",\"focused_goal\":\"...\"}}",
                    provider="minimax",
                    max_tokens=300,
                )
                data = json.loads(resp.strip().lstrip("```json").rstrip("```").strip())
                vs.update_cluster_field(c["cluster_id"], {
                    "core_scenario": data.get("core_scenario", ""),
                    "problem_statement": data.get("problem_statement", ""),
                    "focused_goal": data.get("focused_goal", ""),
                    "updated_at": datetime.utcnow().isoformat(),
                })
                converged += 1
                self.append_log(sub.id, f"  [{i+1}/{len(clusters)}] {topic}: 场景收敛完成")
            except Exception as exc:
                self.append_log(sub.id, f"  [{i+1}/{len(clusters)}] {topic}: 收敛失败（{exc}）")

        return converged

    # ── 工具方法 ────────────────────────────────────────────────────
    def _load_all_requirements(self, vs) -> List[dict]:
        """直接查 req_pool_collection，返回含 topic_l1/l2 的元数据列表"""
        try:
            result = vs.req_pool_collection.get(include=["metadatas"])
            if not result or not result.get("metadatas"):
                return []
            return [m for m in result["metadatas"] if m]
        except Exception as exc:
            return []

    @staticmethod
    def _stable_cluster_id(topic_l1: str, topic_l2: Optional[str], week_iso: str) -> str:
        raw = f"{topic_l1}::{topic_l2 or ''}::{week_iso}"
        return "cls_" + hashlib.md5(raw.encode()).hexdigest()[:12]

    def _create_sub(self, store, parent_id: str, name_suffix: str, title: str) -> AgentTask:
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
