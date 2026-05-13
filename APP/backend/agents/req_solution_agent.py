"""
req_solution_agent.py — 需求 cluster 多方案候选 Agent

接收 cluster_id，生成 3-4 个差异化方案候选，每方案经 GLM 品牌评审：
  Step A: 问题驱动三步分析 → 生成候选方案列表
  Step B: 并行评审（GLM / zhipu）→ 综合分 <0.6 触发重生成（最多 2 次）
  Step C: 写回 cluster.solutions[]，每条含 evidence_links
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND = Path(__file__).resolve().parent.parent
import sys
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.base import AgentStatus, AgentTask, BaseAgent

_LLM_CONFIG_PATH = Path(__file__).resolve().parent.parent / "llm_config.json"


def _load_llm_cfg(provider: str):
    """Return (api_key, model_name, base_url) from llm_config.json for the given provider."""
    try:
        cfg = json.loads(_LLM_CONFIG_PATH.read_text(encoding="utf-8"))
        pcfg = cfg.get(provider, {})
        return pcfg.get("api_key", ""), pcfg.get("model_name", ""), pcfg.get("base_url", "")
    except Exception:
        return "", "", ""

MAX_REGEN_ATTEMPTS = 2
MIN_REVIEW_SCORE = 0.6
TARGET_SOLUTION_COUNT = (3, 4)  # min, max


class ReqSolutionAgent(BaseAgent):
    name = "req_solution"
    display_name = "需求方案生成 Agent"
    capabilities = ["solution-generation", "multi-solution-review"]

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
        return ["solution_generate", "solution_review"]

    # ── 主任务入口 ───────────────────────────────────────────────────
    def run_task(self, task: AgentTask) -> Optional[dict]:
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()
        from llm_service import llm_service

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
        problem = cluster.get("problem_statement") or ""
        goal = cluster.get("focused_goal") or ""
        fact_packet = json.loads(cluster.get("requirement_fact_packet") or "{}")

        # Derive problem/scenario from member requirements if cluster fields are empty
        if not problem or not scenario or scenario == topic:
            try:
                member_ids = json.loads(cluster.get("member_req_ids") or "[]")
                req_summaries = []
                for rid in member_ids[:5]:
                    req = vs.get_requirement(rid)
                    if not req:
                        continue
                    ana = req.get("ai_analysis") or {}
                    if isinstance(ana, str):
                        try:
                            ana = json.loads(ana)
                        except Exception:
                            ana = {}
                    cp = ana.get("core_problem") or req.get("title", "")[:80]
                    if cp:
                        req_summaries.append(cp)
                if req_summaries:
                    if not problem:
                        problem = "；".join(req_summaries[:3])
                    if not scenario or scenario == topic:
                        scenario = req_summaries[0][:200]
            except Exception as e:
                self.append_log(task.id, f"⚠️ 从需求推断问题陈述失败: {e}")

        self.append_log(task.id, f"📐 开始方案生成：{topic}")
        self.report_progress(task.id, 5)

        # ── Step A: 生成候选方案 ──────────────────────────────────
        solutions = []
        for attempt in range(1, MAX_REGEN_ATTEMPTS + 2):
            self.append_log(task.id, f"  生成方案候选（第 {attempt} 次）...")
            raw = self._generate_solutions(llm_service, topic, scenario, problem, goal, fact_packet)
            if raw:
                solutions = raw
                break
            self.append_log(task.id, f"  第 {attempt} 次生成失败，重试...")

        if not solutions:
            self.append_log(task.id, "❌ 方案生成全部失败")
            return {"error": "solution generation failed after retries"}

        self.report_progress(task.id, 40)
        self.append_log(task.id, f"✅ 生成 {len(solutions)} 个候选方案")

        # ── Step B: 并行评审 ──────────────────────────────────────
        review_sub = self._create_sub(store, task.id, "方案质量评审")
        reviewed_solutions = []
        try:
            reviewed_solutions = self._review_solutions(
                review_sub, store, llm_service, solutions, topic, fact_packet, vs, cluster_id
            )
            self._finish_sub(store, review_sub.id, {"reviewed": len(reviewed_solutions)})
        except Exception as exc:
            self._fail_sub(store, review_sub.id, exc)
            self.append_log(task.id, f"⚠️ 评审阶段异常（{exc}），保留原始方案")
            reviewed_solutions = solutions

        self.report_progress(task.id, 85)

        # ── Step C: 写回 cluster ──────────────────────────────────
        vs.update_cluster_field(cluster_id, {
            "solutions": json.dumps(reviewed_solutions, ensure_ascii=False, default=str),
            "solutions_ready": True,
            "status": "solutions_ready",
            "updated_at": datetime.utcnow().isoformat(),
        })

        self.report_progress(task.id, 100)
        self.append_log(task.id, f"✅ {topic}：{len(reviewed_solutions)} 个方案已写入，等待 PM 审核")

        try:
            from feishu_notifier import get_notifier
            get_notifier().send_message(
                f"📐 需求方案生成完成：{topic}\n"
                f"共 {len(reviewed_solutions)} 个候选方案，请前往需求池审核。"
            )
        except Exception:
            pass

        return {"cluster_id": cluster_id, "solutions_count": len(reviewed_solutions)}

    # ── Step A: 三步分析法生成方案 ───────────────────────────────────
    def _generate_solutions(
        self, llm_service, topic: str, scenario: str, problem: str, goal: str, fact_packet: dict
    ) -> List[dict]:
        bip_state = self._summarize_evidence(fact_packet.get("bip_current_state", []), 600)
        competitor = self._summarize_evidence(fact_packet.get("competitor_evidence", []), 800)
        tickets = self._summarize_evidence(fact_packet.get("related_tickets", []), 400)

        prompt = f"""你是一名资深产品经理，请针对以下需求生成 3-4 个差异化产品方案候选。

## 需求主题
{topic}

## 核心使用场景
{scenario}

## 问题陈述
{problem or '（未提供）'}

## 聚焦目标
{goal or '（未提供）'}

## BIP 现状
{bip_state or '（未检索到）'}

## 竞品数据
{competitor or '（未检索到）'}

## 相关工单样本
{tickets or '（未检索到）'}

---
请生成 3-4 个不同方向的方案候选。要求：
1. 每个方案 target_module 不同（指向 BIP 不同功能模块）
2. 方案间设计思路差异明显（不得仅改标题）
3. 每个方案必须引用上述证据（BIP 现状/竞品/工单）作为 evidence_keys
4. 不得写空泛套话，需给出具体功能点

仅返回 JSON 数组，格式：
[
  {{
    "target_module": "模块名",
    "title": "方案简标题",
    "description": "方案详细描述（200-400字）",
    "key_features": ["功能点1", "功能点2"],
    "evidence_keys": ["bip_current_state[0]", "competitor_evidence[1]"]
  }}
]"""

        try:
            api_key, model_name, base_url = _load_llm_cfg("minimax")
            resp = llm_service.call_llm(prompt, provider="minimax", api_key=api_key,
                                        model_name=model_name, base_url=base_url, max_tokens=2000)
            raw = resp.strip()
            if raw.startswith("Error:") or raw.startswith("模型调用"):
                print(f"[req_solution] _generate_solutions LLM error: {raw[:200]}")
                return []
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                print(f"[req_solution] _generate_solutions no JSON array in response: {raw[:300]}")
                return []
            candidates = json.loads(m.group(0))
            if not isinstance(candidates, list):
                return []
            return candidates[:TARGET_SOLUTION_COUNT[1]]
        except Exception as e:
            print(f"[req_solution] _generate_solutions exception: {e}")
            return []

    # ── Step B: 并行评审 ─────────────────────────────────────────────
    def _review_solutions(
        self, sub: AgentTask, store, llm_service,
        solutions: List[dict], topic: str, fact_packet: dict,
        vs, cluster_id: str,
    ) -> List[dict]:
        results = [None] * len(solutions)
        regen_needed = []

        def _review_one(idx: int, sol: dict):
            score, reasoning = self._review_single(llm_service, sol, topic)
            sol["reviewer_scores"] = score
            sol["llm_reasoning_trace"] = reasoning
            sol["overall_score"] = round(
                0.2 * score.get("completeness", 0) +
                0.2 * score.get("evidence_support", 0) +
                0.2 * score.get("depth", 0) +
                0.2 * score.get("consistency", 0) +
                0.2 * score.get("structure", 0),
                4,
            )
            results[idx] = sol
            self.append_log(sub.id, f"  方案{idx+1}「{sol.get('title','?')}」评分: {sol['overall_score']:.2f}")

        threads = [
            threading.Thread(target=_review_one, args=(i, sol), daemon=True)
            for i, sol in enumerate(solutions)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        final = []
        for sol in results:
            if sol is None:
                continue
            if sol.get("overall_score", 1.0) >= MIN_REVIEW_SCORE:
                final.append(sol)
            else:
                self.append_log(sub.id, f"  ⚠️ 方案「{sol.get('title','?')}」综合分 {sol.get('overall_score',0):.2f} < {MIN_REVIEW_SCORE}，标 low_quality 保留")
                sol["low_quality"] = True
                final.append(sol)

        return final

    def _review_single(self, llm_service, sol: dict, topic: str):
        prompt = f"""请评审以下产品方案（主题：{topic}），按 5 个维度打分（每项 0.0-1.0）：
1. completeness（完整性）：是否覆盖场景、功能、约束
2. evidence_support（证据支撑）：是否引用了BIP/竞品/工单证据
3. depth（深度）：是否有具体可落地的功能点，非泛泛而谈
4. consistency（一致性）：是否与主题和场景对齐
5. structure（结构标准）：表述是否清晰，逻辑是否严谨

方案：
目标模块：{sol.get('target_module', '')}
标题：{sol.get('title', '')}
描述：{sol.get('description', '')}
功能点：{', '.join(sol.get('key_features', []))}
证据引用：{', '.join(sol.get('evidence_keys', []))}

仅返回JSON：{{"completeness":0.X,"evidence_support":0.X,"depth":0.X,"consistency":0.X,"structure":0.X,"reasoning":"..."}}"""
        try:
            api_key, model_name, base_url = _load_llm_cfg("zhipu")
            # Fall back to a configured provider if zhipu key is missing
            review_provider = "zhipu"
            if not api_key:
                for _p in ("minimax", "kimi", "openai"):
                    _ak, _mn, _bu = _load_llm_cfg(_p)
                    if _ak:
                        api_key, model_name, base_url, review_provider = _ak, _mn, _bu, _p
                        break
            resp = llm_service.call_llm(prompt, provider=review_provider, api_key=api_key,
                                        model_name=model_name, base_url=base_url, max_tokens=1500)
            m = re.search(r"\{.*\}", resp.strip(), re.DOTALL)
            if not m:
                return {}, ""
            data = json.loads(m.group(0))
            reasoning = data.pop("reasoning", "")
            scores = {k: float(v) for k, v in data.items() if k != "reasoning"}
            return scores, reasoning
        except Exception:
            return {}, ""

    # ── 工具方法 ────────────────────────────────────────────────────
    @staticmethod
    def _summarize_evidence(items: list, max_chars: int) -> str:
        parts = []
        total = 0
        for item in items:
            text = item.get("summary") or item.get("feature") or str(item)[:100]
            if total + len(text) > max_chars:
                break
            parts.append(f"- {text}")
            total += len(text)
        return "\n".join(parts)

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
