import json
import logging
import os
import re
import threading
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from competitor_quality_guardian import CompetitorQualityGuardian
from agents.req_analyst_agent import ReqAnalystAgent


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, "../.."))
DRAFT_ARTIFACT_DIR = os.path.join(PROJECT_ROOT, "design/spec/.draft_artifacts")
DRAFT_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "design/spec")

os.makedirs(DRAFT_ARTIFACT_DIR, exist_ok=True)
os.makedirs(DRAFT_OUTPUT_DIR, exist_ok=True)

logger = logging.getLogger(__name__)


class DraftQualityError(Exception):
    def __init__(
        self,
        summary: str,
        artifact: Optional[Dict[str, Any]] = None,
        issues: Optional[List[str]] = None,
        stage: str = "reviewer",
    ):
        super().__init__(summary)
        self.summary = summary
        self.artifact = artifact or {}
        self.issues = issues or []
        self.stage = stage


class ReqPoolDraftService:
    def __init__(
        self,
        spec_generator,
        vector_store=None,
        kb_runtime_service=None,
        competitor_research_service=None,
        competitor_quality_guardian=None,
        design_fact_service=None,
        analysis_agent=None,
    ):
        self.spec_generator = spec_generator
        self.vector_store = vector_store or getattr(spec_generator, "vector_store", None)
        self.kb_runtime_service = kb_runtime_service
        self.competitor_research_service = competitor_research_service
        self.competitor_quality_guardian = competitor_quality_guardian or CompetitorQualityGuardian()
        self.design_fact_service = design_fact_service
        self.analysis_agent = analysis_agent or (ReqAnalystAgent(self.vector_store) if self.vector_store else None)
        self.tasks: Dict[str, Dict[str, Any]] = {}

    def _call_research_llm_json(self, prompt: str, llm_config: Optional[Dict[str, Any]] = None) -> Any:
        """调用 LLM 并解析 JSON 输出。失败返回 None。"""
        if not self.analysis_agent:
            return None
        try:
            raw = self.analysis_agent._call_llm_sync(prompt, llm_config or {})
            cleaned = self.analysis_agent._strip_model_wrappers(raw)
            json_str = self.analysis_agent._extract_first_json_object(cleaned)
            if not json_str:
                # 尝试提取 JSON 数组
                start = cleaned.find("[")
                if start != -1:
                    end = cleaned.rfind("]")
                    if end > start:
                        json_str = cleaned[start : end + 1]
            if json_str:
                return json.loads(json_str)
        except Exception as e:
            logger.warning("[ReqPoolDraft] _call_research_llm_json failed: %s", e)
        return None

    def _build_requirement_metadata(self, req: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(req or {})
        payload.update(overrides or {})
        return {
            "status": payload.get("status", "new"),
            "source_issues": payload.get("source_issues", []),
            "ai_analysis": payload.get("ai_analysis", {}),
            "review_records": payload.get("review_records", []),
            "entry_source": payload.get("entry_source", ""),
            "requirement_fact_packet": payload.get("requirement_fact_packet", {}),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
        }

    def _artifact_path(self, draft_id: str) -> str:
        return os.path.join(DRAFT_ARTIFACT_DIR, f"{draft_id}.json")

    def _read_artifact(self, draft_id: str) -> Optional[Dict[str, Any]]:
        path = self._artifact_path(draft_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _read_text_file(self, path: str) -> str:
        if not path or not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _write_artifact_file(self, artifact: Dict[str, Any]):
        with open(self._artifact_path(artifact["draft_id"]), "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2)

    def _normalize_artifact_record(self, artifact: Dict[str, Any], run_status: str = "") -> Dict[str, Any]:
        normalized = dict(artifact or {})
        normalized["draft_id"] = normalized.get("draft_id") or str(uuid.uuid4())
        normalized["run_id"] = normalized.get("run_id") or normalized["draft_id"]
        normalized["run_status"] = run_status or normalized.get("run_status") or "completed"
        normalized["created_at"] = normalized.get("created_at") or datetime.now().isoformat()
        normalized["draft_content"] = normalized.get("draft_content") or self._read_text_file(normalized.get("spec_path", ""))
        normalized["draft_excerpt"] = normalized.get("draft_excerpt") or normalized.get("draft_content", "")[:1200]
        normalized["is_current"] = bool(normalized.get("is_current", False))
        normalized["overwrite_confirmation_required"] = bool(normalized.get("overwrite_confirmation_required", False))
        normalized["base_success_run_id"] = normalized.get("base_success_run_id") or ""
        return normalized

    def _load_draft_records(self, req_id: str) -> List[Dict[str, Any]]:
        drafts: List[Dict[str, Any]] = []
        if not os.path.exists(DRAFT_ARTIFACT_DIR):
            return drafts
        for filename in os.listdir(DRAFT_ARTIFACT_DIR):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(DRAFT_ARTIFACT_DIR, filename)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except Exception:
                continue
            if payload.get("req_id") != req_id:
                continue
            drafts.append(self._normalize_artifact_record(payload))
        drafts.sort(key=lambda item: (item.get("created_at", ""), item.get("draft_id", "")), reverse=True)
        return drafts

    def _persist_run_artifact(self, req_id: str, artifact: Dict[str, Any], run_status: str) -> Dict[str, Any]:
        normalized = self._normalize_artifact_record(artifact, run_status=run_status)
        existing_runs = self._load_draft_records(req_id)
        current_success = next(
            (item for item in existing_runs if item.get("is_current") and item.get("run_status") == "completed"),
            None,
        )

        for item in existing_runs:
            if item.get("draft_id") == normalized["draft_id"]:
                continue
            if run_status == "completed":
                item["is_current"] = False
                item["overwrite_confirmation_required"] = False
                self._write_artifact_file(item)
            elif current_success and item.get("overwrite_confirmation_required"):
                item["overwrite_confirmation_required"] = False
                self._write_artifact_file(item)

        if run_status == "completed":
            normalized["is_current"] = True
            normalized["overwrite_confirmation_required"] = False
            normalized["base_success_run_id"] = ""
        else:
            normalized["is_current"] = current_success is None
            normalized["overwrite_confirmation_required"] = current_success is not None
            normalized["base_success_run_id"] = current_success.get("draft_id", "") if current_success else ""

        self._write_artifact_file(normalized)
        return normalized

    def _current_draft_status(self, artifact: Dict[str, Any]) -> str:
        if artifact.get("submitted_to_planning"):
            return "scheduled"
        if artifact.get("ready_to_submit"):
            return "draft_ready"
        if artifact.get("run_status") and artifact.get("run_status") != "completed":
            return "draft_review"
        return artifact.get("draft_status") or "draft_review"

    def _sync_requirement_after_runs(self, req_id: str):
        runs = self._load_draft_records(req_id)
        if not runs or not self.vector_store:
            return
        current = next((item for item in runs if item.get("is_current")), None) or runs[0]
        latest = runs[0]
        pending = next((item for item in runs if item.get("overwrite_confirmation_required")), None)
        self._sync_requirement_with_draft(
            req_id,
            self._current_draft_status(current),
            {
                **current,
                "latest_run_id": latest.get("draft_id"),
                "latest_run_status": latest.get("run_status"),
                "pending_overwrite_draft_id": pending.get("draft_id") if pending else "",
            },
        )

    def _create_agents(self) -> Dict[str, Dict[str, Any]]:
        agents = {}
        for name in ("planner", "researcher", "competitor_researcher", "quality_guardian", "writer", "reviewer"):
            agents[name] = {
                "status": "waiting",
                "started_at": "",
                "finished_at": "",
                "output_summary": "",
                "error": "",
            }
        return agents

    def _create_task(
        self,
        req_id: str,
        draft_type: str,
        mode: str,
        base_draft_id: str = "",
        revision_comments: str = "",
        llm_config: Optional[Dict[str, Any]] = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        self.tasks[task_id] = {
            "task_id": task_id,
            "draft_id": task_id,
            "req_id": req_id,
            "draft_type": draft_type,
            "mode": mode,
            "base_draft_id": base_draft_id,
            "revision_comments": revision_comments,
            "llm_config": dict(llm_config or {}),
            "status": "pending",
            "current_stage": "queued",
            "agents": self._create_agents(),
            "artifact": None,
            "error": "",
            "error_summary": "",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        return task_id

    def _set_agent_state(self, task: Dict[str, Any], agent_name: str, status: str, summary: str = "", error: str = ""):
        agent = task["agents"][agent_name]
        now = datetime.now().isoformat()
        if status == "running":
            agent["started_at"] = now
        if status in {"completed", "failed"}:
            agent["finished_at"] = now
        agent["status"] = status
        if summary:
            agent["output_summary"] = summary
        if error:
            agent["error"] = error
        task["updated_at"] = now

    def start_generation_task(self, req_id: str, draft_type: str, llm_config: Optional[Dict[str, Any]] = None) -> str:
        if draft_type not in {"summary", "detail"}:
            raise ValueError("draft_type must be 'summary' or 'detail'")
        task_id = self._create_task(req_id, draft_type, mode="create", llm_config=llm_config)
        self._set_requirement_status(req_id, "drafting")
        worker = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
        worker.start()
        return task_id

    def start_revision_task(self, req_id: str, draft_id: str, revision_comments: str) -> str:
        if not revision_comments or not revision_comments.strip():
            raise ValueError("revision_comments is required")

        # 自动采集修订意见中的知识到 KB
        if revision_comments:
            try:
                from kb_auto_import import get_auto_import
                _auto_import = get_auto_import()
                if _auto_import:
                    _auto_import.extract_and_save(
                        revision_comments,
                        source_context={'type': 'prd_comment', 'ref_id': req_id or ''},
                    )
            except Exception:
                pass

        artifact = self._read_artifact(draft_id)
        if artifact is None or artifact.get("req_id") != req_id:
            raise ValueError("Draft artifact not found")
        task_id = self._create_task(
            req_id,
            artifact.get("draft_type", "summary"),
            mode="revise",
            base_draft_id=draft_id,
            revision_comments=revision_comments.strip(),
            llm_config=None,
        )
        self._set_requirement_status(req_id, "drafting")
        worker = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
        worker.start()
        return task_id

    def _run_task(self, task_id: str):
        task = self.tasks[task_id]
        task["status"] = "running"
        task["current_stage"] = "planner"
        task["updated_at"] = datetime.now().isoformat()

        try:
            if task["mode"] == "revise":
                artifact = self._run_revision_pipeline(task)
            elif not self.vector_store:
                artifact = self._run_legacy_generation_task(task)
            else:
                artifact = self._run_generation_pipeline(task)

            artifact = self._persist_run_artifact(task["req_id"], artifact, run_status="completed")
            self._sync_requirement_after_runs(task["req_id"])
            task["artifact"] = artifact
            task["status"] = "completed"
            task["current_stage"] = "completed"
            task["error"] = ""
            task["error_summary"] = ""
        except DraftQualityError as exc:
            artifact = exc.artifact or {"draft_id": task["draft_id"], "req_id": task["req_id"], "draft_type": task["draft_type"]}
            artifact.setdefault("draft_status", "draft_review")
            artifact = self._persist_run_artifact(task["req_id"], artifact, run_status="failed")
            self._sync_requirement_after_runs(task["req_id"])
            task["artifact"] = artifact
            task["status"] = "failed"
            task["current_stage"] = exc.stage
            task["error"] = exc.summary
            task["error_summary"] = "；".join(exc.issues) if exc.issues else exc.summary
        except Exception as exc:
            artifact = self._persist_run_artifact(
                task["req_id"],
                {
                    "draft_id": task["draft_id"],
                    "req_id": task["req_id"],
                    "draft_type": task["draft_type"],
                    "draft_status": "draft_review",
                    "pending_questions": [],
                    "quality_assessment": {
                        "decision": "failed",
                        "scores": {"richness": 1, "reliability": 1, "value": 1, "relevance": 1},
                        "blocking_issues": [str(exc)],
                        "summary": str(exc),
                    },
                },
                run_status="failed",
            )
            self._sync_requirement_after_runs(task["req_id"])
            task["artifact"] = artifact
            task["status"] = "failed"
            task["current_stage"] = "failed"
            task["error"] = str(exc)
            task["error_summary"] = str(exc)
        finally:
            task["updated_at"] = datetime.now().isoformat()

    def _run_generation_pipeline(self, task: Dict[str, Any]) -> Dict[str, Any]:
        req = self._load_requirement(task["req_id"])
        req = self._ensure_ai_analysis(req, task.get("llm_config", {}))

        self._set_agent_state(task, "planner", "running")
        plan = self._run_planner(req, task["draft_type"])
        self._set_agent_state(task, "planner", "completed", f"{len(plan['analysis_sections'])} analysis sections planned")

        task["current_stage"] = "researcher"
        self._set_agent_state(task, "researcher", "running")
        research = self._run_researcher(req, plan, task.get("llm_config", {}))
        self._set_agent_state(
            task,
            "researcher",
            "completed",
            f"{research['ticket_summary']['matched_ticket_count']} tickets, {research['evidence_bundle'].get('matched_count', 0)} evidence items",
        )

        task["current_stage"] = "competitor_researcher"
        self._set_agent_state(task, "competitor_researcher", "running")
        research = self._run_competitor_researcher(req, research)
        competitor_count = len((research.get("competitor_comparison") or {}).get("vendors", []) or [])
        self._set_agent_state(
            task,
            "competitor_researcher",
            "completed",
            f"{competitor_count} competitors researched with {sum(len(v.get('citations', [])) for v in (research.get('competitor_comparison') or {}).get('vendors', []))} citations",
        )

        task["current_stage"] = "quality_guardian"
        self._set_agent_state(task, "quality_guardian", "running")
        try:
            research, bundle_assessment = self._run_quality_guardian_bundle(req, research)
        except DraftQualityError as exc:
            self._set_agent_state(task, "quality_guardian", "failed", error=exc.summary)
            raise

        task["current_stage"] = "writer"
        self._set_agent_state(task, "writer", "running")
        artifact = self._run_writer(task["draft_id"], req, plan, research, task["draft_type"])
        self._set_agent_state(task, "writer", "completed", artifact.get("spec_file", "draft generated"))

        task["current_stage"] = "quality_guardian"
        try:
            artifact, final_quality = self._run_quality_guardian_artifact(artifact, bundle_assessment)
        except DraftQualityError as exc:
            self._set_agent_state(task, "quality_guardian", "failed", error=exc.summary)
            raise
        self._set_agent_state(task, "quality_guardian", "completed", final_quality.get("summary", "quality gate passed"))

        task["current_stage"] = "reviewer"
        self._set_agent_state(task, "reviewer", "running")
        try:
            review = self._run_reviewer(artifact)
        except DraftQualityError as exc:
            self._set_agent_state(task, "reviewer", "failed", error=exc.summary)
            raise
        artifact["review_notes"] = review["review_notes"]
        artifact["pending_questions"] = list(dict.fromkeys((artifact.get("pending_questions") or []) + review["pending_questions"]))
        artifact["draft_status"] = "draft_review"
        self._set_agent_state(task, "reviewer", "completed", review["summary"])
        return artifact

    def _run_revision_pipeline(self, task: Dict[str, Any]) -> Dict[str, Any]:
        req = self._load_requirement(task["req_id"])
        req = self._ensure_ai_analysis(req, task.get("llm_config", {}))
        base_artifact = self._read_artifact(task["base_draft_id"])
        if not base_artifact:
            raise ValueError("Base draft artifact not found")

        self._set_agent_state(task, "planner", "running")
        plan = self._run_planner(req, task["draft_type"])
        self._set_agent_state(task, "planner", "completed", "reuse previous draft plan")

        task["current_stage"] = "researcher"
        self._set_agent_state(task, "researcher", "running")
        _rev_comments = (task.get("revision_comments") or "").strip()
        if _rev_comments:
            # 有审核意见 → 无条件重跑 researcher，把意见和轮次透传给所有 _build_* 方法
            _llm_cfg = dict(task.get("llm_config") or {})
            _llm_cfg["revision_comments"] = _rev_comments
            _llm_cfg["revision_iteration"] = base_artifact.get("revision_iteration", 0) + 1
            research = self._run_researcher(req, plan, _llm_cfg)
        else:
            research = self._artifact_to_research(base_artifact)
            if not research.get("ticket_summary"):
                research = self._run_researcher(req, plan, task.get("llm_config") or {})
        self._set_agent_state(
            task,
            "researcher",
            "completed",
            f"reused {research['ticket_summary'].get('matched_ticket_count', 0)} tickets and {research['evidence_bundle'].get('matched_count', 0)} evidence items",
        )

        task["current_stage"] = "competitor_researcher"
        self._set_agent_state(task, "competitor_researcher", "running")
        research = self._run_competitor_researcher(req, research, base_artifact=base_artifact)
        self._set_agent_state(
            task,
            "competitor_researcher",
            "completed",
            "reuse competitor evidence bundle" if (base_artifact.get("analysis_packet", {}) or {}).get("competitor_comparison") else "competitor bundle refreshed",
        )

        task["current_stage"] = "quality_guardian"
        self._set_agent_state(task, "quality_guardian", "running")
        try:
            research, bundle_assessment = self._run_quality_guardian_bundle(req, research)
        except DraftQualityError as exc:
            self._set_agent_state(task, "quality_guardian", "failed", error=exc.summary)
            raise

        task["current_stage"] = "writer"
        self._set_agent_state(task, "writer", "running")
        artifact = self._run_rewriter(task["draft_id"], req, plan, research, base_artifact, task["revision_comments"])
        self._set_agent_state(task, "writer", "completed", artifact.get("spec_file", "revised draft"))

        task["current_stage"] = "quality_guardian"
        try:
            artifact, final_quality = self._run_quality_guardian_artifact(artifact, bundle_assessment)
        except DraftQualityError as exc:
            self._set_agent_state(task, "quality_guardian", "failed", error=exc.summary)
            raise
        self._set_agent_state(task, "quality_guardian", "completed", final_quality.get("summary", "quality gate passed"))

        task["current_stage"] = "reviewer"
        self._set_agent_state(task, "reviewer", "running")
        try:
            review = self._run_reviewer(artifact)
        except DraftQualityError as exc:
            self._set_agent_state(task, "reviewer", "failed", error=exc.summary)
            raise
        artifact["review_notes"] = review["review_notes"]
        artifact["pending_questions"] = list(dict.fromkeys((artifact.get("pending_questions") or []) + review["pending_questions"]))
        artifact["draft_status"] = "draft_review"
        self._set_agent_state(task, "reviewer", "completed", review["summary"])

        # T3: 持久化 revision_history + revision_iteration
        _rev = (task.get("revision_comments") or "").strip()
        _prev_history = list(base_artifact.get("revision_history") or [])
        if _rev:
            _new_iter = len(_prev_history) + 1
            artifact["revision_history"] = _prev_history + [{
                "iteration": _new_iter,
                "comments": _rev,
                "timestamp": datetime.now().isoformat(),
                "changed_sections": ["solution_candidates", "change_impact", "upstream_downstream_analysis"],
            }]
            artifact["revision_iteration"] = _new_iter
        else:
            artifact["revision_history"] = _prev_history
            artifact["revision_iteration"] = base_artifact.get("revision_iteration", 0)

        # T6: 计算各 LLM 章节 diff，写回 artifact
        artifact["section_diffs"] = self._compute_section_diff(base_artifact, artifact)

        return artifact

    def _run_legacy_generation_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        self._set_agent_state(task, "planner", "running")
        self._set_agent_state(task, "planner", "completed", "legacy planner shim")
        self._set_agent_state(task, "researcher", "running")
        self._set_agent_state(task, "researcher", "completed", "legacy researcher shim")
        self._set_agent_state(task, "competitor_researcher", "running")
        self._set_agent_state(task, "competitor_researcher", "completed", "legacy competitor shim")
        self._set_agent_state(task, "quality_guardian", "running")
        self._set_agent_state(task, "quality_guardian", "completed", "legacy quality shim")
        self._set_agent_state(task, "writer", "running")
        artifact = self.spec_generator.generate_draft_artifact(task["req_id"], task["draft_type"])
        artifact["draft_id"] = task["draft_id"]
        artifact["version"] = 1
        artifact["submitted_to_planning"] = False
        artifact["ready_to_submit"] = False
        artifact["draft_status"] = "draft_review"
        artifact["quality_assessment"] = {
            "target_type": "draft_content",
            "decision": "passed",
            "scores": {"richness": 3, "reliability": 3, "value": 3, "relevance": 3},
            "blocking_issues": [],
            "improvement_actions": [],
            "summary": "legacy draft",
        }
        artifact["created_at"] = artifact.get("created_at") or datetime.now().isoformat()
        self._set_agent_state(task, "writer", "completed", artifact.get("spec_file", "legacy draft"))
        self._set_agent_state(task, "reviewer", "running")
        review = self._run_reviewer(artifact)
        artifact["review_notes"] = review["review_notes"]
        artifact["pending_questions"] = list(dict.fromkeys((artifact.get("pending_questions") or []) + review["pending_questions"]))
        self._set_agent_state(task, "reviewer", "completed", review["summary"])
        return artifact

    def _load_requirement(self, req_id: str) -> Dict[str, Any]:
        if not self.vector_store:
            raise ValueError("Vector store not configured for draft service")
        req = self.vector_store.get_requirement(req_id)
        if not req:
            raise ValueError("Requirement not found.")
        if req.get("status") not in {"new", "to_review", "drafting", "draft_review", "draft_ready", "scheduled"}:
            raise ValueError("Requirement must be in 'new', 'drafting', 'draft_review', 'draft_ready' or 'scheduled' status.")
        return req

    def _normalize_module_hint(self, value: str, fallback: str = "") -> str:
        value = (value or "").strip()
        if not value or value in {"待确认", "未知", "相关模块"}:
            return fallback
        return value

    def _normalize_ai_analysis(self, analysis_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        analysis = dict(analysis_result or {})
        root_cause = str(analysis.get("root_cause", "") or "").strip()
        module = str(analysis.get("module", "") or "").strip()
        mvp_suggestion = str(analysis.get("mvp_suggestion", "") or "").strip()
        core_problem = str(analysis.get("core_problem", "") or "").strip()
        current_product_behavior = str(analysis.get("current_product_behavior", "") or "").strip()
        gap_analysis = str(analysis.get("gap_analysis", "") or "").strip()
        product_layer = str(analysis.get("product_layer", "") or "").strip()
        scenario_keywords = analysis.get("scenario_keywords", [])
        if isinstance(scenario_keywords, str):
            scenario_keywords = [item.strip() for item in re.split(r"[、,，/\n]+", scenario_keywords) if item.strip()]
        elif not isinstance(scenario_keywords, list):
            scenario_keywords = []
        scenario_keywords = [str(item).strip() for item in scenario_keywords if str(item).strip()]

        parse_failure_markers = [
            "解析失败",
            "expecting value",
            "char 0",
            "jsondecodeerror",
        ]
        combined_text = f"{core_problem}\n{gap_analysis}\n{root_cause}\n{mvp_suggestion}".lower()

        if any(marker in combined_text for marker in parse_failure_markers):
            analysis["core_problem"] = "模型未返回有效的结构化分析结果，请重试。"
            analysis["current_product_behavior"] = "待确认，当前未获得稳定的产品现状分析。"
            analysis["gap_analysis"] = "模型未返回有效的结构化分析结果，请重试。"
            analysis["root_cause"] = "模型未返回有效的结构化分析结果，请重试。"
            analysis["module"] = "待确认"
            analysis["product_layer"] = "待确认"
            analysis["scenario_keywords"] = []
            analysis["mvp_suggestion"] = "本次自动分析未形成可用结论，请检查模型配置或稍后重试。"
            return analysis

        if not root_cause and not core_problem and not mvp_suggestion:
            analysis["core_problem"] = "模型未返回有效的结构化分析结果，请重试。"
            analysis["current_product_behavior"] = current_product_behavior or "待确认，当前未获得稳定的产品现状分析。"
            analysis["gap_analysis"] = gap_analysis or "模型未返回有效的结构化分析结果，请重试。"
            analysis["root_cause"] = "模型未返回有效的结构化分析结果，请重试。"
            analysis["module"] = module or "待确认"
            analysis["product_layer"] = product_layer or "待确认"
            analysis["scenario_keywords"] = scenario_keywords
            analysis["mvp_suggestion"] = "本次自动分析未形成可用结论，请检查模型配置或稍后重试。"
            return analysis

        if module == "未知" and ("api key" not in combined_text and "未配置" not in combined_text):
            analysis["module"] = "待确认"

        analysis["core_problem"] = core_problem or root_cause or mvp_suggestion
        analysis["current_product_behavior"] = current_product_behavior
        analysis["gap_analysis"] = gap_analysis or root_cause or core_problem
        analysis["product_layer"] = product_layer or "待确认"
        analysis["scenario_keywords"] = self._dedupe_terms(scenario_keywords)[:8]

        return analysis

    def _build_ai_analysis_evidence_bundle(self, req: Dict[str, Any], analysis_result: Dict[str, Any], llm_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.kb_runtime_service:
            return {}

        summary_parts = [req.get("title", "").strip(), req.get("description", "").strip()[:1000]]
        summary = "\n".join(part for part in summary_parts if part).strip()
        module_hint = analysis_result.get("module", "")

        try:
            evidence_bundle = self.kb_runtime_service.analyze(
                summary=summary,
                module_hint=module_hint,
                top_k=8,
                llm_config=llm_config or {},
            )
        except Exception as exc:
            logger.warning("[ReqPoolDraft] build ai_analysis evidence failed for %s: %s", req.get("req_id"), exc)
            return {}
        return evidence_bundle or {}

    def _persist_requirement_ai_analysis(self, req: Dict[str, Any], analysis_result: Dict[str, Any]) -> Dict[str, Any]:
        updated = dict(req or {})
        updated["ai_analysis"] = analysis_result
        updated["updated_at"] = datetime.now().isoformat()
        if self.vector_store:
            self.vector_store.upsert_requirement(
                updated["req_id"],
                updated.get("title", ""),
                updated.get("description", ""),
                self._build_requirement_metadata(updated, {"updated_at": updated["updated_at"]}),
            )
        return updated

    def _ensure_ai_analysis(self, req: Dict[str, Any], llm_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        current = dict(req.get("ai_analysis", {}) or {})
        needs_core_analysis = not (
            current.get("root_cause")
            and current.get("module")
            and current.get("mvp_suggestion")
            and current.get("core_problem")
            and current.get("gap_analysis")
            and current.get("product_layer")
        )

        if needs_core_analysis and self.analysis_agent:
            current = self._normalize_ai_analysis(self.analysis_agent.analyze(req, llm_config or {}))
        else:
            current = self._normalize_ai_analysis(current)

        evidence_bundle = self._build_ai_analysis_evidence_bundle(req, current, llm_config)
        if evidence_bundle:
            current = {
                **current,
                "module_hint": current.get("module", ""),
                "topic_ids": evidence_bundle.get("topic_ids", []),
                "topic_names": evidence_bundle.get("topic_names", []),
                "evidence_bundle": evidence_bundle,
            }

        if self.design_fact_service:
            try:
                knowledge_context = self.design_fact_service.build_requirement_context(
                    {**req, "ai_analysis": current},
                    evidence_bundle=evidence_bundle,
                )
            except Exception as exc:
                logger.warning("[ReqPoolDraft] build design context failed for %s: %s", req.get("req_id"), exc)
                knowledge_context = {}
            current = {
                **current,
                "design_fact_bundle": knowledge_context.get("design_fact_bundle", current.get("design_fact_bundle", {})),
                "competitor_dossiers": knowledge_context.get("competitor_dossiers", current.get("competitor_dossiers", [])),
            }

        return self._persist_requirement_ai_analysis(req, current)

    def _infer_module_from_text(self, text: str) -> str:
        mapping = [
            ("流程监控", "流程监控"),
            ("未来审批", "流程预测/未来审批"),
            ("流程预测", "流程预测/未来审批"),
            ("审批面板", "审批面板"),
            ("流程图", "流程图"),
            ("流程调度", "流程调度"),
            ("工作流设计", "工作流设计"),
        ]
        for keyword, module in mapping:
            if keyword in text:
                return module
        return "流程中心"

    def _infer_customer_type_from_text(self, text: str) -> str:
        if any(keyword in text for keyword in ["报销", "付款", "请假", "采购", "单据", "费用"]):
            return "单据审批客户"
        if any(keyword in text for keyword in ["协同", "消息", "友空间", "待办"]):
            return "协同审批客户"
        if any(keyword in text for keyword in ["监控", "预测", "流程图", "审批面板"]):
            return "流程治理客户"
        return "流程应用客户"

    def _run_planner(self, req: Dict[str, Any], draft_type: str) -> Dict[str, Any]:
        clues = self._extract_requirement_clues(req)
        inferred_module = (clues.get("modules", []) or [""])[0] or self._infer_module_from_text(clues.get("text", ""))
        module_hint = self._normalize_module_hint(
            req.get("ai_analysis", {}).get("module_hint") or req.get("ai_analysis", {}).get("module", ""),
            fallback=inferred_module,
        )
        return {
            "outline_type": "evidence-first-business-analysis-dossier",
            "draft_type": draft_type,
            "module_hint": module_hint,
            "analysis_sections": [
                {
                    "id": "background",
                    "title": "需求背景与问题定义",
                    "required_fields": ["ticket_summary", "business_scenarios"],
                },
                {
                    "id": "business_scenarios",
                    "title": "业务场景拆解",
                    "required_fields": ["business_scenarios", "source_citations"],
                },
                {
                    "id": "ticket_statistics",
                    "title": "全量工单统计与典型案例",
                    "required_fields": ["ticket_summary", "ticket_appendix"],
                },
                {
                    "id": "customer_analysis",
                    "title": "客户业务特征分析",
                    "required_fields": ["customer_profiles"],
                },
                {
                    "id": "capability_analysis",
                    "title": "现有能力与缺口分析",
                    "required_fields": ["capability_analysis"],
                },
                {
                    "id": "upstream_downstream",
                    "title": "上下游结合点与场景连通性",
                    "required_fields": ["upstream_downstream_analysis"],
                },
                {
                    "id": "change_impact",
                    "title": "功能改动点与影响面",
                    "required_fields": ["change_impact"],
                },
                {
                    "id": "solution_architecture",
                    "title": "方案与功能架构参考",
                    "required_fields": ["solution_candidates", "functional_architecture"],
                },
                {
                    "id": "risk_assessment",
                    "title": "风险点与改动量粗估",
                    "required_fields": ["risk_summary", "effort_estimate_size"],
                },
                {
                    "id": "pending_decisions",
                    "title": "待人工确认项",
                    "required_fields": ["pending_questions"],
                },
                {
                    "id": "prd_mapping",
                    "title": "PRD映射建议",
                    "required_fields": ["change_impact", "solution_candidates"],
                },
                {
                    "id": "ticket_appendix",
                    "title": "工单全量附录",
                    "required_fields": ["ticket_appendix"],
                },
            ],
            "prd_mapping_sections": [
                "目标与范围",
                "模块影响面",
                "关键能力",
                "交互与展示",
                "边界与验收建议",
            ] if draft_type == "summary" else [
                "目标与范围",
                "模块影响面",
                "详细功能拆解",
                "交互与边界说明",
                "验收建议",
            ],
        }

    def _normalize_ticket_match(self, item: Dict[str, Any]) -> Dict[str, Any]:
        metadata = item.get("metadata", {}) or {}
        summary = item.get("summary", "")
        document_excerpt = (item.get("document") or item.get("document_excerpt") or "")[:400]
        combined_text = " ".join([summary, document_excerpt, metadata.get("labels", ""), metadata.get("module", "")])
        customer_type = (
            metadata.get("customer_type")
            or metadata.get("customer_issue_type")
            or metadata.get("project_desc")
            or metadata.get("project")
            or self._infer_customer_type_from_text(combined_text)
        )
        module = metadata.get("module") or metadata.get("team") or metadata.get("domain") or self._infer_module_from_text(combined_text)
        return {
            "issue_key": item.get("issue_key") or metadata.get("issue_key") or item.get("content_id", ""),
            "summary": summary,
            "score": float(item.get("score", 0.0) or 0.0),
            "status": metadata.get("status", ""),
            "module": module,
            "customer_type": customer_type,
            "labels": metadata.get("labels", ""),
            "document_excerpt": document_excerpt,
            "metadata": metadata,
        }

    def _extract_source_issue_keys(self, req: Dict[str, Any]) -> List[str]:
        extracted: List[str] = []
        for item in req.get("source_issues", []) or []:
            if isinstance(item, dict):
                issue_key = item.get("issue_key") or item.get("key")
            else:
                issue_key = str(item)
            if issue_key:
                extracted.append(issue_key)
        return extracted

    def _load_source_issue_matches(self, req: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.vector_store or not hasattr(self.vector_store, "get_issue_by_key"):
            return []

        source_matches: List[Dict[str, Any]] = []
        for issue_key in self._extract_source_issue_keys(req):
            try:
                item = self.vector_store.get_issue_by_key(issue_key)
            except Exception:
                item = None
            if not item:
                continue
            normalized = self._normalize_ticket_match(
                {
                    **item,
                    "issue_key": item.get("issue_key") or issue_key,
                    "score": float(item.get("score", 0.0) or 0.0) or 1.25,
                }
            )
            normalized["score"] = max(float(normalized.get("score", 0.0) or 0.0), 1.25)
            normalized["forced_keep"] = True
            source_matches.append(normalized)
        return source_matches

    def _extract_requirement_clues(self, req: Dict[str, Any]) -> Dict[str, Any]:
        text = "\n".join(
            part
            for part in [
                req.get("title", ""),
                req.get("description", ""),
                req.get("ai_analysis", {}).get("root_cause", ""),
                req.get("ai_analysis", {}).get("mvp_suggestion", ""),
            ]
            if part
        )
        labels = re.findall(r"标签[:：]\s*([^\n]+)", text)
        modules = re.findall(r"(?:领域模块|模块)[:：]\s*([^\n]+)", text)
        issue_types = re.findall(r"(?:客户问题类型|研发确认问题类型)[:：]\s*([^\n]+)", text)
        return {
            "labels": [item.strip() for group in labels for item in re.split(r"[、,，/ ]+", group) if item.strip()],
            "modules": [item.strip() for item in modules if item.strip()],
            "issue_types": [item.strip() for item in issue_types if item.strip()],
            "text": text,
        }

    def _dedupe_terms(self, items: List[str]) -> List[str]:
        ordered: List[str] = []
        seen = set()
        for item in items:
            item = (item or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    def _comment_focus_markers(self) -> List[str]:
        return ["附言", "说明", "备注", "意见", "评论", "修正说明", "补充说明", "修改意见", "提交意见", "处理意见", "文本弹窗"]

    def _submission_focus_markers(self) -> List[str]:
        return ["重新提交", "重提", "二次提交", "再次提交", "驳回后重新提交", "退回后重新提交", "提交时", "提交前", "单据提交", "发起提交", "发起", "提交"]

    def _comment_query_objects(self) -> List[str]:
        return ["提交意见", "处理意见", "备注", "附言", "说明", "补充说明", "修改意见", "修正说明"]

    def _comment_input_markers(self) -> List[str]:
        return ["填写", "输入", "录入", "编辑", "补充", "增加", "弹窗", "文本框", "有个框", "留言"]

    def _is_comment_capture_requirement(self, text: str = "", focus: Optional[Dict[str, Any]] = None) -> bool:
        haystack = text or ""
        if focus:
            haystack = " ".join(
                [
                    haystack,
                    " ".join(focus.get("actions", []) or []),
                    " ".join(focus.get("objects", []) or []),
                    " ".join(focus.get("constraints", []) or []),
                ]
            )
        has_resubmit = any(term in haystack for term in self._submission_focus_markers())
        has_comment = any(term in haystack for term in self._comment_focus_markers())
        return has_resubmit and has_comment

    def _build_comment_capture_queries(self, req: Dict[str, Any], focus: Dict[str, Any]) -> List[str]:
        text = "\n".join(
            [
                req.get("title", ""),
                req.get("description", ""),
                (req.get("ai_analysis", {}) or {}).get("root_cause", ""),
                (req.get("ai_analysis", {}) or {}).get("mvp_suggestion", ""),
            ]
        )
        has_resubmit = any(term in text for term in ["重新提交", "重提", "二次提交", "再次提交", "驳回后", "退回后"])
        has_submit = any(term in text for term in ["提交", "发起", "提交时", "提交前"])

        action_queries: List[str] = []
        if has_resubmit:
            action_queries.extend(["重新提交", "二次提交", "驳回后重新提交"])
        if has_submit:
            action_queries.extend(["提交时", "单据提交"])
        if not action_queries:
            action_queries.extend(["重新提交", "提交时"])

        queries: List[str] = []
        if has_resubmit:
            queries.extend(
                [
                    "重新提交意见填写",
                    "重新提交 提交意见",
                    "重新提交 处理意见",
                    "重新提交 备注",
                    "二次提交 修正说明",
                    "重新提交 文本弹窗 说明",
                    "发起人修正后再次重新提交 说明",
                    "驳回后重新提交 说明",
                ]
            )
        if has_submit:
            queries.extend(
                [
                    "提交时 填写意见",
                    "提交时 备注",
                    "提交时 附言",
                    "审批流 查看 提交意见",
                    "审批记录 提交意见",
                ]
            )

        if "后续审批人可见" in (focus.get("constraints") or []):
            queries.extend(["审批流 查看 补充说明", "后续审批人 查看 提交意见"])

        for action in self._dedupe_terms(action_queries)[:3]:
            for obj in self._comment_query_objects()[:6]:
                queries.append(f"{action} {obj}")

        return self._dedupe_terms(queries)

    def _extract_requirement_focus(
        self,
        req: Dict[str, Any],
        module_hint: str = "",
        evidence_bundle: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        clues = self._extract_requirement_clues(req)
        title = req.get("title", "")
        description = req.get("description", "")
        ai_analysis = req.get("ai_analysis", {}) or {}
        evidence_bundle = dict(evidence_bundle or ai_analysis.get("evidence_bundle", {}) or {})
        invalid_ai_markers = [
            "API Key 未配置",
            "无法进行结构化需求分析",
            "模型未返回有效的结构化分析结果",
            "当前未获得稳定的产品现状分析",
        ]
        analysis_parts = [
            ai_analysis.get("core_problem", ""),
            ai_analysis.get("current_product_behavior", ""),
            ai_analysis.get("gap_analysis", ""),
            ai_analysis.get("root_cause", ""),
            ai_analysis.get("mvp_suggestion", ""),
        ]
        text = "\n".join(
            part
            for part in [title, description, *analysis_parts]
            if part and not any(marker in str(part) for marker in invalid_ai_markers)
        )

        scenario_keywords = ai_analysis.get("scenario_keywords", []) or []
        if isinstance(scenario_keywords, str):
            scenario_keywords = [item.strip() for item in re.split(r"[、,，/\n]+", scenario_keywords) if item.strip()]
        elif not isinstance(scenario_keywords, list):
            scenario_keywords = []
        scenario_keywords = self._dedupe_terms([str(item).strip() for item in scenario_keywords if str(item).strip()])

        product_layer = str(ai_analysis.get("product_layer", "") or "").strip()
        layer_to_surface = {
            "运行时": ["运行时", "审批面板", "流程图", "单据流程"],
            "租户级": ["租户级", "租户配置", "企业级配置", "全局开关"],
            "流程级": ["流程级", "流程设计器", "流程属性", "流程配置"],
            "节点级": ["节点级", "节点属性", "环节配置", "节点配置"],
            "跨层": ["跨层", "跨层联动"],
        }

        action_groups = {
            "重新提交": ["重新提交", "重提", "再次提交", "重新提交流程"],
            "填写": ["填写", "录入", "补充", "追加", "输入"],
            "展示": ["展示", "显示", "带给", "传递", "透传", "可见"],
            "校验": ["校验", "必填", "必须", "拦截", "校验规则"],
        }
        object_groups = {
            "附言": ["附言", "备注", "说明", "意见", "附言说明", "审批说明", "审批意见", "重提说明"],
            "审批人": ["审批人", "后续审批人", "处理人"],
            "流程监控": ["流程监控", "监控"],
            "未来审批": ["未来审批", "未来审批流", "流程预测", "候选审批人"],
        }
        surface_groups = {
            "审批面板": ["审批面板", "审批页", "审批界面", "待办处理区"],
            "重提弹窗": ["重提弹窗", "重新提交弹窗", "提交弹窗"],
            "流程图": ["流程图", "链路图"],
            "流程监控": ["流程监控", "监控详情"],
        }
        constraint_groups = {
            "必填": ["必填", "必须填写", "不能为空"],
            "后续审批人可见": ["后续审批人", "后续节点", "可见", "透传"],
            "重新提交触发": ["重新提交时", "重提时", "提交前"],
        }

        def collect(groups: Dict[str, List[str]]) -> List[str]:
            hits: List[str] = []
            for canonical, variants in groups.items():
                if any(variant in text for variant in variants):
                    hits.append(canonical)
            return hits

        actions = collect(action_groups)
        objects = collect(object_groups)
        surfaces = collect(surface_groups)
        constraints = collect(constraint_groups)
        comment_like = self._is_comment_capture_requirement(text)

        normalized_module_hint = self._normalize_module_hint(module_hint, fallback="")
        manifest_hits = self._manifest_capability_hits(req, normalized_module_hint, evidence_bundle)
        manifest_terms = self._dedupe_terms(
            (evidence_bundle.get("topic_names", []) or [])
            + [item.get("name", "") for item in manifest_hits[:3]]
            + [item.get("summary", "") for item in manifest_hits[:3]]
            + [term for item in manifest_hits[:3] for term in (item.get("matched_terms", []) or [])]
        )
        manifest_text = " ".join(manifest_terms)
        future_approval_like = any(marker in f"{text}\n{manifest_text}" for marker in ["未来审批", "未来审批流", "流程预测", "候选审批人"])

        if not product_layer or product_layer == "待确认":
            if any(marker in f"{text}\n{manifest_text}" for marker in ["审批面板", "流程图", "流程监控", "监控", "查询", "展示", "未来审批", "流程预测", "候选审批人"]):
                product_layer = "运行时"
            elif any(marker in text for marker in ["租户", "企业级", "全局开关"]):
                product_layer = "租户级"
            elif any(marker in text for marker in ["流程设计器", "流程属性", "流程配置"]):
                product_layer = "流程级"
            elif any(marker in text for marker in ["节点", "环节", "签署环节", "审批环节"]):
                product_layer = "节点级"

        if not scenario_keywords:
            fallback_keywords: List[str] = []
            if future_approval_like:
                fallback_keywords.extend(["未来审批流", "流程预测", "候选审批人", "审批面板", "流程图"])
            if any(marker in f"{text}\n{manifest_text}" for marker in ["流程监控", "监控"]):
                fallback_keywords.extend(["流程监控", "监控详情"])
            fallback_keywords.extend(evidence_bundle.get("topic_names", []) or [])
            fallback_keywords.extend((clues.get("modules", []) or [])[:3])
            if normalized_module_hint:
                fallback_keywords.append(normalized_module_hint)
            scenario_keywords = self._dedupe_terms([term for term in fallback_keywords if term])[:8]

        if comment_like:
            objects = [item for item in objects if item != "审批人"]
            if "附言" not in objects:
                objects.insert(0, "附言")
            if any(term in text for term in ["审批人", "后续审批人", "可见", "透传", "带给"]):
                constraints.append("后续审批人可见")

        if future_approval_like:
            surfaces.extend(["审批面板", "流程图"])
            objects.append("审批人")
            constraints.append("后续审批人可见")

        for canonical, variants in surface_groups.items():
            if any(variant in manifest_text for variant in variants):
                surfaces.append(canonical)
        if "流程预测" in manifest_text and "流程图" not in surfaces:
            surfaces.append("流程图")
        if "流程预测" in manifest_text and "审批面板" not in surfaces:
            surfaces.append("审批面板")

        scenario_action_hints = [keyword for keyword in scenario_keywords if any(marker in keyword for marker in ["提交", "录入", "填写", "展示", "查看", "透传", "校验", "配置", "查询"])]
        scenario_surface_hints = [keyword for keyword in scenario_keywords if any(marker in keyword for marker in ["面板", "弹窗", "流程", "节点", "环节", "租户", "表单", "单据", "监控", "设计器"])]
        scenario_object_hints = [keyword for keyword in scenario_keywords if keyword not in scenario_action_hints and keyword not in scenario_surface_hints]

        actions.extend(scenario_action_hints[:4])
        objects.extend(scenario_object_hints[:4])
        surfaces.extend(scenario_surface_hints[:4])
        surfaces.extend(layer_to_surface.get(product_layer, []))

        if module_hint:
            surfaces.append(module_hint)
        surfaces.extend(clues.get("modules", []) or [])
        if evidence_bundle:
            surfaces.extend(evidence_bundle.get("topic_names", []) or [])

        query_phrases: List[str] = []
        for keyword in scenario_keywords[:6]:
            query_phrases.append(keyword)
            if module_hint:
                query_phrases.append(f"{module_hint} {keyword}".strip())
            if product_layer and product_layer != "待确认":
                query_phrases.append(f"{product_layer} {keyword}".strip())

        for action in actions[:3] or [""]:
            for obj in objects[:3]:
                if action:
                    query_phrases.append(f"{action}{obj}")
                    query_phrases.append(f"{action} {obj}")
                else:
                    query_phrases.append(obj)
                for surface in surfaces[:2]:
                    query_phrases.append(f"{surface} {action} {obj}".strip())
        if comment_like:
            query_phrases.extend(
                [
                    "重新提交意见填写",
                    "重新提交 修改意见",
                    "二次提交 修正说明",
                    "重新提交 文本弹窗 说明",
                    "发起人修正后再次重新提交 说明",
                    "驳回后重新提交 说明",
                ]
            )
        query_phrases.extend(constraints[:2])
        query_phrases.extend(clues.get("labels", []) or [])
        query_phrases.extend(clues.get("issue_types", []) or [])

        primary_terms = self._dedupe_terms(
            scenario_keywords
            + actions
            + objects
            + surfaces
            + constraints
            + (clues.get("labels", []) or [])[:4]
        )
        query_phrases = self._dedupe_terms([title, *query_phrases, " ".join(primary_terms[:4])])
        return {
            "actions": self._dedupe_terms(actions),
            "objects": self._dedupe_terms(objects),
            "surfaces": self._dedupe_terms(surfaces),
            "constraints": self._dedupe_terms(constraints),
            "primary_terms": primary_terms,
            "query_phrases": query_phrases[:12],
            "summary": "、".join(self._dedupe_terms((scenario_keywords[:3] or []) + actions[:2] + objects[:2] + surfaces[:2])) or (title or "当前需求"),
            "scenario_keywords": scenario_keywords[:8],
            "product_layer": product_layer or "待确认",
        }

    def _build_ticket_queries(self, req: Dict[str, Any]) -> List[str]:
        clues = self._extract_requirement_clues(req)
        title = req.get("title", "").strip()
        description = req.get("description", "").strip()
        labels = clues.get("labels", []) or []
        modules = clues.get("modules", []) or []
        focus = self._extract_requirement_focus(req)
        comment_like = self._is_comment_capture_requirement("", focus)
        source_issue_matches = self._load_source_issue_matches(req)
        source_terms: List[str] = []
        for item in source_issue_matches[:3]:
            source_terms.append(item.get("summary", ""))
            source_terms.append(item.get("module", ""))
            source_terms.extend(re.split(r"[、,，/ ]+", item.get("labels", "")))

        scenario_keywords = focus.get("scenario_keywords", []) or []
        structured_queries: List[str] = []
        for keyword in scenario_keywords[:6]:
            structured_queries.append(keyword)
            if focus.get("product_layer") and focus.get("product_layer") != "待确认":
                structured_queries.append(f"{focus.get('product_layer')} {keyword}".strip())
            for surface in (focus.get("surfaces") or [])[:2]:
                structured_queries.append(f"{surface} {keyword}".strip())

        if comment_like and scenario_keywords:
            queries = [*structured_queries, *focus.get("query_phrases", [])[:6]]
            queries.extend(
                phrase
                for phrase in self._build_comment_capture_queries(req, focus)
                if any(keyword in phrase for keyword in scenario_keywords[:4])
            )
            source_comment_terms = [
                term
                for term in self._dedupe_terms(source_terms)
                if any(marker in term for marker in self._comment_focus_markers())
                or any(marker in term for marker in self._submission_focus_markers())
            ]
            if source_comment_terms:
                queries.append(" ".join(source_comment_terms[:6]).strip())
            return self._dedupe_terms(queries)[:10]

        if comment_like and not scenario_keywords:
            queries = self._build_comment_capture_queries(req, focus)
            queries.extend(
                phrase
                for phrase in focus.get("query_phrases", [])[:6]
                if self._is_comment_capture_requirement(phrase)
            )
            source_comment_terms = [
                term
                for term in self._dedupe_terms(source_terms)
                if any(marker in term for marker in self._comment_focus_markers())
                or any(marker in term for marker in self._submission_focus_markers())
            ]
            if source_comment_terms:
                queries.append(" ".join(source_comment_terms[:6]).strip())
            return self._dedupe_terms(queries)[:10]

        queries = [title, *structured_queries[:6], *focus.get("query_phrases", [])[:6]]
        queries.extend(
            [
                f"{title} {' '.join(labels[:3])}".strip(),
                f"{title} {' '.join(modules[:2])}".strip(),
                " ".join(focus.get("primary_terms", [])[:5]).strip(),
                " ".join(self._dedupe_terms(source_terms)[:6]).strip(),
            ]
        )
        if not comment_like:
            queries.append(description[:180])
        return self._dedupe_terms(queries)[:10]

    def _merge_ticket_matches(self, matched_groups: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for group in matched_groups:
            for item in group:
                normalized = self._normalize_ticket_match(item)
                issue_key = normalized.get("issue_key")
                if not issue_key:
                    continue
                existing = merged.get(issue_key)
                if not existing or normalized.get("score", 0.0) > existing.get("score", 0.0):
                    merged[issue_key] = normalized
        return sorted(merged.values(), key=lambda item: (-item.get("score", 0.0), item.get("issue_key", "")))

    def _build_ticket_appendix(self, req: Dict[str, Any]) -> List[Dict[str, Any]]:
        appendix: List[Dict[str, Any]] = []
        matched_groups: List[List[Dict[str, Any]]] = []
        source_issue_matches = self._load_source_issue_matches(req)
        if source_issue_matches:
            matched_groups.append(source_issue_matches)
        if self.vector_store and hasattr(self.vector_store, "search_similar_issues"):
            for query in self._build_ticket_queries(req):
                try:
                    matched_groups.append(self.vector_store.search_similar_issues(query, top_k=20, min_score=0.05) or [])
                except Exception:
                    continue

        appendix = self._merge_ticket_matches(matched_groups)
        known_keys = {item["issue_key"] for item in appendix if item.get("issue_key")}
        for issue_key in self._extract_source_issue_keys(req):
            if issue_key in known_keys:
                continue
            appendix.append(
                {
                    "issue_key": issue_key,
                    "summary": "原始来源工单",
                    "score": 1.0,
                    "status": "",
                    "module": req.get("ai_analysis", {}).get("module", "未标注模块"),
                    "customer_type": "来源工单",
                    "labels": "",
                    "document_excerpt": "",
                    "metadata": {},
                }
            )

        appendix.sort(key=lambda item: (-item.get("score", 0.0), item.get("issue_key", "")))
        return appendix

    def _apply_ticket_soft_filter(self, appendix: List[Dict[str, Any]], req: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not appendix:
            return [], {
                "matched_ticket_count_raw": 0,
                "filtered_low_relevance_count": 0,
                "source_issue_count": len(self._extract_source_issue_keys(req)),
            }

        focus = self._extract_requirement_focus(req)
        source_issue_keys = set(self._extract_source_issue_keys(req))
        comment_like = self._is_comment_capture_requirement("", focus)
        comment_markers = self._comment_focus_markers()
        submission_markers = self._submission_focus_markers()
        resubmit_markers = ["重新提交", "重提", "二次提交", "再次重新提交", "驳回后", "退回后"]
        input_markers = self._comment_input_markers()
        view_markers = ["查看", "显示", "可见", "留痕", "审批记录", "流程中", "同步"]
        approval_context_markers = ["审批流", "审批", "审批面板", "后续审批人", "审批记录", "流程中"]
        kept: List[Dict[str, Any]] = []
        filtered: List[Dict[str, Any]] = []

        for item in appendix:
            normalized_score = float(item.get("score", 0.0) or 0.0)
            relevance_score = normalized_score if normalized_score > 1 else normalized_score * 100
            enriched = dict(item)
            enriched["forced_keep"] = enriched.get("issue_key") in source_issue_keys
            haystack = " ".join([item.get("summary", ""), item.get("document_excerpt", ""), item.get("module", ""), item.get("labels", "")])
            action_hits = sum(1 for term in focus.get("actions", []) if term in haystack)
            object_hits = sum(1 for term in focus.get("objects", []) if term in haystack)
            surface_hits = sum(1 for term in focus.get("surfaces", []) if term in haystack)
            phrase_hits = sum(1 for term in focus.get("query_phrases", [])[:6] if term and term in haystack)
            comment_hits = sum(1 for marker in comment_markers if marker in haystack)
            submission_hits = sum(1 for marker in submission_markers if marker in haystack)
            resubmit_hits = sum(1 for marker in resubmit_markers if marker in haystack)
            input_hits = sum(1 for marker in input_markers if marker in haystack)
            view_hits = sum(1 for marker in view_markers if marker in haystack)
            approval_context_hits = sum(1 for marker in approval_context_markers if marker in haystack)
            negative_hits = sum(
                1 for marker in ["无关", "不相关", "无直接关系", "关联较弱", "低相关", "unrelated"]
                if marker in haystack
            )
            negated_comment = 1 if re.search(r"(不涉及|无关|不相关|未涉及).{0,12}(附言|备注|说明|意见|留言)", haystack) else 0
            effective_comment_hits = 0 if negated_comment else comment_hits
            adjusted_score = relevance_score + action_hits * 18 + object_hits * 22 + surface_hits * 10 + phrase_hits * 20 - negative_hits * 80 - negated_comment * 120
            if comment_like:
                adjusted_score += effective_comment_hits * 28
                adjusted_score += submission_hits * 16
                adjusted_score += input_hits * 18
                adjusted_score += view_hits * 8
                adjusted_score += approval_context_hits * 10
                if effective_comment_hits == 0:
                    adjusted_score -= 120
                if submission_hits == 0:
                    adjusted_score -= 40
                if input_hits == 0 and phrase_hits == 0:
                    adjusted_score -= 90
            if action_hits == 0 and object_hits == 0 and phrase_hits == 0 and (not comment_like or comment_hits == 0):
                adjusted_score -= 35
            enriched["relevance_score"] = round(adjusted_score, 1)
            enriched["focus_hits"] = {
                "actions": action_hits,
                "objects": object_hits,
                "surfaces": surface_hits,
                "phrases": phrase_hits,
                "comment_markers": comment_hits,
                "submission_markers": submission_hits,
                "resubmit_markers": resubmit_hits,
                "input_markers": input_hits,
                "view_markers": view_hits,
                "approval_context": approval_context_hits,
                "negative": negative_hits,
                "negated_comment": negated_comment,
            }
            if comment_like:
                has_strong_focus = phrase_hits > 0 or (
                    effective_comment_hits > 0 and submission_hits > 0 and input_hits > 0 and approval_context_hits > 0
                )
                has_partial_focus = effective_comment_hits > 0 and input_hits > 0 and (submission_hits > 0 or approval_context_hits > 0)
            else:
                has_strong_focus = phrase_hits > 0 or (action_hits > 0 and object_hits > 0)
                has_partial_focus = action_hits > 0 or object_hits > 0 or phrase_hits > 0
            if enriched["forced_keep"] or has_strong_focus or (adjusted_score >= 75 and has_partial_focus):
                kept.append(enriched)
            else:
                filtered.append(enriched)

        kept.sort(
            key=lambda item: (
                0 if item.get("forced_keep") else 1,
                -float(item.get("relevance_score", 0.0) or 0.0),
                item.get("issue_key", ""),
            )
        )
        return kept, {
            "matched_ticket_count_raw": len(appendix),
            "filtered_low_relevance_count": max(len(appendix) - len(kept), 0),
            "source_issue_count": sum(1 for item in kept if item.get("issue_key") in source_issue_keys),
        }

    def _bootstrap_source_issues_from_matches(self, req: Dict[str, Any], ticket_appendix: List[Dict[str, Any]]) -> List[str]:
        if self._extract_source_issue_keys(req):
            return self._extract_source_issue_keys(req)

        focus = self._extract_requirement_focus(req)
        comment_like = self._is_comment_capture_requirement("", focus)
        suggested: List[str] = []
        for item in ticket_appendix:
            issue_key = item.get("issue_key")
            if not issue_key:
                continue
            relevance_score = float(item.get("relevance_score", 0.0) or 0.0)
            focus_hits = item.get("focus_hits", {}) or {}
            comment_hits = int(focus_hits.get("comment_markers", 0) or 0)
            submission_hits = int(focus_hits.get("submission_markers", 0) or 0)
            if relevance_score >= 95 or (
                comment_like
                and submission_hits > 0
                and comment_hits > 0
                and relevance_score >= 70
            ):
                suggested.append(issue_key)
        return self._dedupe_terms(suggested)[:3]

    def _persist_bootstrapped_source_issues(self, req: Dict[str, Any], source_issue_keys: List[str]) -> None:
        if not source_issue_keys or self._extract_source_issue_keys(req):
            return
        req["source_issues"] = list(source_issue_keys)
        if not self.vector_store or not hasattr(self.vector_store, "upsert_requirement"):
            return
        current = {}
        if hasattr(self.vector_store, "get_requirement") and req.get("req_id"):
            try:
                current = self.vector_store.get_requirement(req["req_id"]) or {}
            except Exception:
                current = {}
        payload = {
            **current,
            **req,
            "source_issues": list(source_issue_keys),
            "updated_at": datetime.now().isoformat(),
        }
        try:
            self.vector_store.upsert_requirement(
                payload.get("req_id", req.get("req_id")),
                payload.get("title", req.get("title", "")),
                payload.get("description", req.get("description", "")),
                self._build_requirement_metadata(payload),
            )
        except Exception:
            return

    def _build_ticket_summary(self, appendix: List[Dict[str, Any]], stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        module_counter = Counter(item.get("module", "未标注模块") for item in appendix)
        customer_counter = Counter(item.get("customer_type", "未标注客户类型") for item in appendix)
        stats = stats or {}
        typical_case_keys = [item["issue_key"] for item in appendix[:5] if item.get("issue_key")]
        return {
            "matched_ticket_count": len(appendix),
            "matched_ticket_count_raw": int(stats.get("matched_ticket_count_raw", len(appendix)) or 0),
            "filtered_low_relevance_count": int(stats.get("filtered_low_relevance_count", 0) or 0),
            "source_issue_count": int(stats.get("source_issue_count", 0) or 0),
            "top_modules": [{"name": name, "count": count} for name, count in module_counter.most_common(5)],
            "customer_type_distribution": [{"name": name, "count": count} for name, count in customer_counter.most_common(5)],
            "typical_case_keys": typical_case_keys,
            "top_issue_keys": typical_case_keys,
        }

    def _build_customer_profiles(self, req: Dict[str, Any], appendix: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        focus = self._extract_requirement_focus(req)
        for item in appendix:
            label = item.get("customer_type") or "未标注客户类型"
            grouped.setdefault(label, []).append(item)

        profiles = []
        focus_summary = focus.get("summary") or "当前需求场景"
        surfaces = "、".join((focus.get("surfaces") or [])[:3])
        objects = "、".join((focus.get("objects") or [])[:3])
        for label, items in sorted(grouped.items(), key=lambda pair: len(pair[1]), reverse=True)[:4]:
            module_counter = Counter(item.get("module", "未标注模块") for item in items)
            modules_text = "、".join(name for name, _ in module_counter.most_common(3)) or "待补充模块"
            reason_fragments = [focus_summary]
            if surfaces:
                reason_fragments.append(f"重点触点为 {surfaces}")
            if objects:
                reason_fragments.append(f"核心对象为 {objects}")
            profiles.append(
                {
                    "label": label,
                    "ticket_count": len(items),
                    "typical_modules": [name for name, _ in module_counter.most_common(3)],
                    "representative_tickets": [item["issue_key"] for item in items[:3] if item.get("issue_key")],
                    "analysis": f"{label}相关工单共 {len(items)} 条，问题集中在 {modules_text}，"
                    f"主要诉求围绕 {'；'.join(reason_fragments)}。",
                }
            )
        return profiles

    def _build_related_requirements(self, req: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.vector_store or not hasattr(self.vector_store, "search_similar_requirements"):
            return []
        items_by_req_id: Dict[str, Dict[str, Any]] = {}
        for query in self._build_ticket_queries(req)[:6]:
            try:
                related = self.vector_store.search_similar_requirements(query, top_k=8) or []
            except Exception:
                related = []
            for item in related:
                req_id = item.get("req_id")
                if not req_id or req_id == req.get("req_id"):
                    continue
                normalized = {
                    "req_id": req_id,
                    "title": item.get("title", ""),
                    "score": float(item.get("score", 0.0) or 0.0),
                    "status": item.get("status", ""),
                }
                existing = items_by_req_id.get(req_id)
                if not existing or normalized["score"] > existing["score"]:
                    items_by_req_id[req_id] = normalized
        items = sorted(items_by_req_id.values(), key=lambda item: (-item["score"], item["req_id"]))
        return items[:6]

    def _capability_terms_for_requirement(self, req: Dict[str, Any], module_hint: str, evidence_bundle: Dict[str, Any]) -> List[str]:
        clues = self._extract_requirement_clues(req)
        ai_analysis = req.get("ai_analysis", {}) or {}
        scenario_keywords = ai_analysis.get("scenario_keywords", []) or []
        if isinstance(scenario_keywords, str):
            scenario_keywords = [item.strip() for item in re.split(r"[、,，/\n]+", scenario_keywords) if item.strip()]
        elif not isinstance(scenario_keywords, list):
            scenario_keywords = []
        analysis_terms = [
            ai_analysis.get("core_problem", ""),
            ai_analysis.get("current_product_behavior", ""),
            ai_analysis.get("gap_analysis", ""),
            ai_analysis.get("root_cause", ""),
            ai_analysis.get("module", ""),
            ai_analysis.get("product_layer", ""),
        ]
        terms: List[str] = []
        terms.extend([str(item).strip() for item in scenario_keywords if str(item).strip()])
        terms.extend([str(item).strip() for item in analysis_terms if str(item).strip()])
        terms.extend(clues.get("labels", []) or [])
        terms.extend(clues.get("modules", []) or [])
        normalized_module_hint = self._normalize_module_hint(module_hint, fallback="")
        terms.extend([normalized_module_hint] if normalized_module_hint else [])
        terms.extend(evidence_bundle.get("topic_names", [])[:4] if evidence_bundle.get("topic_names") else [])
        seen = set()
        ordered = []
        for term in terms:
            term = (term or "").strip()
            if not term or term in seen:
                continue
            seen.add(term)
            ordered.append(term)
        return ordered[:16]

    def _score_capability_item(self, item: Dict[str, Any], terms: List[str]) -> tuple[int, List[str]]:
        haystack = " ".join(
            [
                item.get("name", ""),
                item.get("summary", ""),
                item.get("source_rel_path", ""),
                item.get("source_path", ""),
                " ".join(item.get("keywords", []) or []),
            ]
        )
        matched_terms = [term for term in terms if term and term in haystack]
        if not matched_terms:
            return 0, []
        score = len(set(matched_terms))
        if "流程中心" in haystack:
            score += 2
        if item.get("source_kind") == "kb_local":
            score += 1
        return score, matched_terms

    def _manifest_capability_hits(self, req: Dict[str, Any], module_hint: str, evidence_bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.kb_runtime_service or not hasattr(self.kb_runtime_service, "get_manifest"):
            return []
        try:
            manifest = self.kb_runtime_service.get_manifest()
        except Exception:
            return []
        terms = self._capability_terms_for_requirement(req, module_hint, evidence_bundle)
        scored: List[Dict[str, Any]] = []
        for item in manifest.get("items", []) or []:
            score, matched_terms = self._score_capability_item(item, terms)
            if score <= 0:
                continue
            enriched = dict(item)
            enriched["score"] = float(enriched.get("score", 0.0) or 0.0) + score
            enriched["matched_terms"] = matched_terms
            scored.append(enriched)
        scored.sort(key=lambda item: (-float(item.get("score", 0.0)), item.get("name", "")))
        return scored[:16]

    def _ticket_capability_hits(self, appendix: List[Dict[str, Any]], terms: List[str]) -> List[Dict[str, Any]]:
        hits = []
        for item in appendix:
            haystack = " ".join([item.get("summary", ""), item.get("document_excerpt", ""), item.get("module", ""), item.get("labels", "")])
            matched_terms = [term for term in terms if term and term in haystack]
            if not matched_terms:
                continue
            hits.append(
                {
                    "content_id": item.get("issue_key", ""),
                    "name": item.get("summary", item.get("issue_key", "相关工单")),
                    "summary": item.get("document_excerpt", "") or item.get("summary", ""),
                    "source_kind": "ticket_case",
                    "citation_label": f"[TICKET] {item.get('issue_key', '')}",
                    "score": item.get("score", 0.0) + len(set(matched_terms)),
                    "matched_terms": matched_terms,
                }
            )
        return hits

    def _select_focus_tickets(self, appendix: List[Dict[str, Any]], req: Dict[str, Any]) -> List[Dict[str, Any]]:
        title = req.get("title", "")
        focus = self._extract_requirement_focus(req)
        priority_terms = focus.get("primary_terms", [])[:8]
        scored = []
        for item in appendix:
            haystack = " ".join([item.get("summary", ""), item.get("document_excerpt", ""), item.get("module", ""), item.get("labels", "")])
            matched = [term for term in priority_terms if term in haystack or term in title]
            score = item.get("score", 0.0) + len(matched) * 0.5
            scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].get("issue_key", "")))
        return [item for _, item in scored[:5]]

    def _derive_background_root_cause(self, req: Dict[str, Any], research: Dict[str, Any]) -> str:
        raw = (req.get("ai_analysis", {}).get("root_cause") or "").strip()
        invalid_markers = ["模型未返回有效", "解析失败", "请重试"]
        if raw and not any(marker in raw for marker in invalid_markers):
            return raw
        scenarios = research.get("business_scenarios", []) or []
        focus = self._extract_requirement_focus(req, research.get("module_hint", ""), research.get("evidence_bundle", {}))
        if scenarios:
            return scenarios[0].get("summary", "").strip() or f"当前需求聚焦 {focus.get('summary') or '当前场景'}。"
        gaps = research.get("capability_analysis", {}).get("coverage_gaps", []) or []
        if gaps:
            return gaps[0]
        return f"当前需求聚焦 {focus.get('summary') or '当前场景'}。"

    def _build_internal_references(self, research: Dict[str, Any]) -> List[Dict[str, Any]]:
        references: List[Dict[str, Any]] = []
        capability_items = (research.get("capability_analysis", {}) or {}).get("current_capabilities", []) or []
        for item in capability_items[:6]:
            title = item.get("title") or item.get("name") or "未命名内部资料"
            references.append(
                {
                    "title": title,
                    "source": item.get("citation_label") or item.get("source") or item.get("source_kind") or "internal",
                    "reason": item.get("summary") or item.get("description") or "",
                }
            )

        design_fact_bundle = research.get("design_fact_bundle", {}) or {}
        for key in ("design_principles", "process_rules", "step_rules", "tenant_params", "document_properties"):
            for item in (design_fact_bundle.get(key, []) or [])[:4]:
                references.append(
                    {
                        "title": item.get("name") or "未命名设计事实",
                        "source": item.get("citation_label") or key,
                        "reason": item.get("summary") or "",
                    }
                )

        deduped: List[Dict[str, Any]] = []
        seen = set()
        for item in references:
            key = (item.get("title", ""), item.get("source", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:6]

    def _build_external_references(self, competitor_bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
        references: List[Dict[str, Any]] = []
        for vendor in (competitor_bundle or {}).get("vendors", []) or []:
            verification_status = ((vendor.get("verification") or {}).get("status") or "unverified").strip()
            for citation in (vendor.get("citations") or [])[:3]:
                references.append(
                    {
                        "title": citation.get("title") or citation.get("url") or f"{vendor.get('vendor', '未知厂商')} 资料",
                        "source": vendor.get("vendor") or "external",
                        "reason": vendor.get("implementation_summary") or f"验证状态：{verification_status}",
                        "verification_status": verification_status,
                    }
                )
        deduped: List[Dict[str, Any]] = []
        seen = set()
        for item in references:
            key = (item.get("title", ""), item.get("source", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:6]

    def _build_ticket_evidence_summary(self, research: Dict[str, Any]) -> Dict[str, Any]:
        ticket_summary = research.get("ticket_summary", {}) or {}
        matched_ticket_count = int(ticket_summary.get("matched_ticket_count", 0) or 0)
        customer_profiles = research.get("customer_profiles", []) or []
        business_scenarios = research.get("business_scenarios", []) or []

        representative_tickets = self._dedupe_terms(
            list(ticket_summary.get("typical_case_keys", []) or [])
            + list(ticket_summary.get("top_issue_keys", []) or [])
            + [ticket for profile in customer_profiles for ticket in (profile.get("representative_tickets") or [])]
        )[:5]
        customer_signals = self._dedupe_terms(
            [item.get("label", "") for item in customer_profiles if item.get("label")]
            + [item.get("name", "") for item in (ticket_summary.get("customer_type_distribution", []) or []) if item.get("name")]
        )[:4]
        scenario_titles = [item.get("title", "") for item in business_scenarios if item.get("title")]

        if matched_ticket_count > 0:
            value_statement = (
                f"已命中 {matched_ticket_count} 条高相关工单，"
                f"可作为{'、'.join(customer_signals) or '目标客户'}这类诉求的客户价值证据。"
            )
        else:
            value_statement = "当前缺少高相关工单样本，客户价值证据仍需继续补充。"

        if scenario_titles:
            scenario_statement = (
                f"可从工单中抽象出{ '、'.join(f'"{title}"' for title in scenario_titles[:2]) }等业务场景，"
                "用于指导方案边界和交互设计。"
            )
        elif representative_tickets:
            scenario_statement = "当前已沉淀代表工单，可进一步从具体案例中抽象业务场景与流程差异。"
        else:
            scenario_statement = "当前还缺少足够的工单证据来稳定抽象业务场景。"

        return {
            "positioning": "工单证据用于证明客户需求价值并抽象业务场景，不直接作为现有能力资料。",
            "value_statement": value_statement,
            "scenario_statement": scenario_statement,
            "matched_ticket_count": matched_ticket_count,
            "representative_tickets": representative_tickets,
            "customer_signals": customer_signals,
        }

    def _build_product_value_llm(
        self,
        req: Dict[str, Any],
        research: Dict[str, Any],
        focus_summary: str,
        customer_labels: List[str],
        matched_ticket_count: int,
    ) -> Optional[Dict[str, Any]]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        customer_hint = "、".join(customer_labels[:2]) or "目标客户"
        prompt = (
            "你是产品价值分析师。为以下需求评估三个维度的产品价值，每条 20-50 字。\n\n"
            f"【需求标题】{req.get('title', '')}\n"
            f"【核心问题】{core_problem}\n"
            f"【目标客户】{customer_hint}\n"
            f"【核心场景】{focus_summary}\n"
            f"【相关工单数量】{matched_ticket_count}\n\n"
            "维度说明：\n"
            "- problem_coverage：本需求可覆盖的用户诉求范围和场景类型\n"
            "- customer_value：为目标客户带来的直接价值（减少什么痛点、提升什么效率）\n"
            "- long_term_value：对产品能力体系的长期积累价值\n\n"
            '请只输出合法 JSON：\n'
            '{"problem_coverage": "...", "customer_value": "...", "long_term_value": "..."}'
        )
        llm_result = self._call_research_llm_json(prompt, getattr(self, "_current_llm_config", None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("customer_value"):
            return {
                "problem_coverage": (llm_result.get("problem_coverage") or "").strip(),
                "customer_value": (llm_result.get("customer_value") or "").strip(),
                "long_term_value": (llm_result.get("long_term_value") or "").strip(),
            }
        return None

    def _build_analysis_summary(self, req: Dict[str, Any], research: Dict[str, Any]) -> Dict[str, Any]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        focus = self._extract_requirement_focus(req, research.get("module_hint", ""), research.get("evidence_bundle", {}))
        ticket_summary = research.get("ticket_summary", {}) or {}
        matched_ticket_count = int(ticket_summary.get("matched_ticket_count", 0) or 0)
        customer_profiles = research.get("customer_profiles", []) or []
        customer_labels = [item.get("label", "") for item in customer_profiles if item.get("label")]
        scenario_keywords = ai_analysis.get("scenario_keywords", []) or focus.get("scenario_keywords", []) or []
        if isinstance(scenario_keywords, str):
            scenario_keywords = [item.strip() for item in re.split(r"[、,，/\n]+", scenario_keywords) if item.strip()]
        internal_references = self._build_internal_references(research)
        external_references = self._build_external_references(research.get("competitor_comparison", {}) or {})
        ticket_evidence_summary = self._build_ticket_evidence_summary(research)
        surface_markers = ["面板", "弹窗", "流程图", "监控", "设计器", "配置", "属性", "节点", "环节", "页面", "租户", "入口", "表单", "单据"]
        source_text = "\n".join(
            [
                req.get("title", ""),
                req.get("description", ""),
                ai_analysis.get("core_problem", ""),
                ai_analysis.get("current_product_behavior", ""),
                ai_analysis.get("gap_analysis", ""),
                ai_analysis.get("root_cause", ""),
                " ".join(ai_analysis.get("scenario_keywords", []) or []) if isinstance(ai_analysis.get("scenario_keywords", []), list) else str(ai_analysis.get("scenario_keywords", "") or ""),
            ]
        )
        surface_candidates = [
            str(item).strip()
            for item in ((focus.get("surfaces") or []) + ([research.get("module_hint")] if research.get("module_hint") else []))
            if str(item).strip() and any(marker in str(item) for marker in surface_markers) and str(item).strip() in source_text
        ]
        if not surface_candidates and research.get("module_hint"):
            surface_candidates = [str(research.get("module_hint")).strip()]
        primary_surfaces = self._dedupe_terms(surface_candidates)[:3]
        business_scenario_titles = [item.get("title", "") for item in (research.get("business_scenarios", []) or []) if item.get("title")]
        focus_summary = focus.get("summary") or req.get("title") or "当前需求"

        _pv = self._build_product_value_llm(
            req, research, focus_summary, customer_labels, matched_ticket_count
        )
        if _pv:
            problem_coverage = _pv["problem_coverage"]
            customer_value = _pv["customer_value"]
            long_term_value = _pv["long_term_value"]
        else:
            if matched_ticket_count > 0:
                problem_coverage = f"可直接覆盖 {matched_ticket_count} 条高相关工单，优先还原同类业务诉求与落地边界。"
            else:
                problem_coverage = "当前缺少足够高相关工单，需要继续补充样本以验证问题覆盖面。"
            customer_value = f"帮助{'、'.join(customer_labels[:2]) or '目标客户'}在 {focus_summary} 场景下减少线下解释、返工和支持介入成本。"
            long_term_value = (
                f"把能力沉淀到 {ai_analysis.get('product_layer') or focus.get('product_layer') or '合适层级'}，"
                f"可支撑后续相似需求复用，而不是继续按单点工单补丁演进。"
            )

        return {
            "core_problem": ai_analysis.get("core_problem") or self._derive_background_root_cause(req, research),
            "current_product_behavior": ai_analysis.get("current_product_behavior") or "待确认，需结合现有能力资料继续核实。",
            "gap_analysis": ai_analysis.get("gap_analysis") or ai_analysis.get("root_cause") or "待确认当前能力缺口。",
            "root_cause": ai_analysis.get("root_cause") or self._derive_background_root_cause(req, research),
            "module_hint": research.get("module_hint") or ai_analysis.get("module") or "待确认",
            "product_layer": ai_analysis.get("product_layer") or focus.get("product_layer") or "待确认",
            "scenario_keywords": self._dedupe_terms([str(item).strip() for item in scenario_keywords if str(item).strip()])[:8],
            "primary_surfaces": primary_surfaces,
            "business_scenario_titles": business_scenario_titles[:4],
            "risk_summary": list(research.get("risk_summary", []) or [])[:4],
            "product_value": {
                "problem_coverage": problem_coverage,
                "customer_value": customer_value,
                "long_term_value": long_term_value,
            },
            "ticket_evidence_summary": ticket_evidence_summary,
            "internal_references": internal_references,
            "external_references": external_references,
        }

    # ======================================================================
    # 方案生成 · 问题驱动三步分析法（Problem-Driven Three-Step Analysis）
    # ----------------------------------------------------------------------
    # 设计原则：固定"分析步骤"，不固定"输出维度"。LLM 先定位问题本质、
    # 再盘点现有产品模块、最后按"模块 × 改造动作"产出方案。
    # 关联：design/spec/req-pool-solution-problem-driven-generation.md
    # ======================================================================

    # 层级套话词 —— 出现在 title/description 时视为模板化退化（可作为模块名一部分存在）
    _LAYER_BAN_PATTERNS = [
        re.compile(r"(?<![^\s，。：；（(])运行时(?![^\s，。：；)）])"),
        re.compile(r"(?<![^\s，。：；（(])租户级(?![^\s，。：；)）])"),
        re.compile(r"(?<![^\s，。：；（(])流程级(?![^\s，。：；)）])"),
        re.compile(r"(?<![^\s，。：；（(])节点级(?![^\s，。：；)）])"),
        re.compile(r"(?<![^\s，。：；（(])跨层(?![^\s，。：；)）])"),
    ]
    # 允许句式：title 包含「在<模块>…」或「新增<模块/能力>」；
    # 实质性约束交给「层级禁用词」和「target_module 差异化」两道校验完成
    _TITLE_VERB_PATTERN = re.compile(r"(在\s*[^\s，。：；]|新增\s*[^\s，。：；])")
    _REQUIREMENT_TYPES = {"能力缺失", "交互优化", "配置扩展", "集成补齐", "治理规则", "性能", "其他"}

    def _collect_module_candidates_for_prompt(self, evidence_bundle: Dict[str, Any], module_hint: str) -> str:
        """从 KB topics + evidence 合成"候选模块"注入块。

        优先级：
          1. evidence_bundle.primary_materials 里的模块名（真实命中的高权重资料）
          2. evidence_bundle.topic_names（命中的 topic 层级）
          3. kb_runtime_service.get_topics() 里与 module_hint 或工作流相关的 topic
        去重后输出 3-8 行："- <模块名>: <一句话用途>"。
        """
        candidates: "list[tuple[str, str]]" = []
        seen_names: set = set()

        def _push(name: str, why: str) -> None:
            name = (name or "").strip()
            if not name or name in seen_names:
                return
            seen_names.add(name)
            candidates.append((name, (why or "").strip()))

        # 1. primary_materials：命中的高权重资料（它们的 name 常包含产品模块名）
        for item in (evidence_bundle.get("primary_materials") or [])[:6]:
            name = item.get("name") or item.get("title") or ""
            summary = (item.get("summary") or "")[:60]
            if name:
                _push(name, summary or "命中的产品资料")

        # 2. topic_names：命中 topic 的中文名
        for topic_name in (evidence_bundle.get("topic_names") or [])[:6]:
            _push(topic_name, "知识库 topic 匹配")

        # 3. KB 全量 topic：按 module_hint 或工作流关键词过滤
        if len(candidates) < 4 and self.kb_runtime_service and hasattr(self.kb_runtime_service, "get_topics"):
            try:
                topics = self.kb_runtime_service.get_topics() or []
            except Exception:
                topics = []
            hint_tokens = [tok for tok in re.split(r"[\s/，、]+", module_hint or "") if tok]
            for topic in topics:
                name = topic.get("name") or ""
                topic_id = topic.get("topic_id") or ""
                if not name or name in seen_names:
                    continue
                # 偏向工作流/流程/审批相关 topic
                hay = f"{name} {topic_id} {' '.join(topic.get('keywords') or [])}"
                hit = any(tok and tok in hay for tok in hint_tokens) or any(
                    kw in hay for kw in ("工作流", "流程", "审批", "业务活动", "表单", "规则")
                )
                if hit:
                    keywords = "、".join((topic.get("keywords") or [])[:3])
                    _push(name, f"topic {topic_id}" + (f"（关键词：{keywords}）" if keywords else ""))
                if len(candidates) >= 8:
                    break

        # 4. 兜底：如果还是太少，补一些通用工作流模块
        if len(candidates) < 3:
            fallback = [
                ("工作流设计器", "流程定义入口，改造流程模板、环节规则、分支条件"),
                ("流程监控", "运行时实例的查询/干预/补办入口"),
                ("审批矩阵", "审批人/角色/岗位规则配置中心"),
                ("业务活动", "单据级与业务对象级的行为配置"),
            ]
            for name, why in fallback:
                _push(name, why)
                if len(candidates) >= 4:
                    break

        if not candidates:
            return "- （当前无可用候选模块，请在方案中说明『新增 <模块名>』及其理由）"

        return "\n".join(f"- {name}：{why}" for name, why in candidates[:8])

    def _collect_similar_cases_for_prompt(
        self,
        evidence_bundle: Dict[str, Any],
        related_requirements: List[Dict[str, Any]],
    ) -> str:
        lines: List[str] = []
        for item in (evidence_bundle.get("primary_materials") or [])[:2]:
            name = item.get("name") or item.get("title") or ""
            summary = (item.get("summary") or "").replace("\n", " ")[:120]
            if name and summary:
                lines.append(f"- [资料] {name}：{summary}")
            elif name:
                lines.append(f"- [资料] {name}")
        for item in (related_requirements or [])[:3]:
            title = item.get("title", "")
            req_id = item.get("req_id", "")
            if title:
                lines.append(f"- [相似需求] {req_id} {title}")
        if not lines:
            return "- （无相似历史案例）"
        return "\n".join(lines)

    def _char_ngrams(self, text: str, n: int = 3) -> set:
        text = re.sub(r"\s+", "", text or "")
        if len(text) < n:
            return {text} if text else set()
        return {text[i : i + n] for i in range(len(text) - n + 1)}

    def _validate_solution_diversity(
        self,
        candidates: List[Dict[str, Any]],
        threshold: float = 0.7,
    ) -> "tuple[bool, str]":
        """两两比较 description 的字符 3-gram Jaccard 相似度，超过阈值判定同质化。"""
        descriptions = [(c.get("description") or "") for c in candidates]
        grams = [self._char_ngrams(desc, 3) for desc in descriptions]
        for i in range(len(grams)):
            for j in range(i + 1, len(grams)):
                a, b = grams[i], grams[j]
                if not a or not b:
                    continue
                inter = len(a & b)
                union = len(a | b)
                score = inter / union if union else 0.0
                if score >= threshold:
                    t_i = candidates[i].get("title") or f"方案{i+1}"
                    t_j = candidates[j].get("title") or f"方案{j+1}"
                    return False, f"方案《{t_i}》与《{t_j}》的 description 相似度 {score:.2f} ≥ {threshold}（同质化）"
        return True, ""

    def _validate_title_format(self, candidates: List[Dict[str, Any]]) -> "tuple[bool, str]":
        """每条 title 必须是「在<模块>增加/改造/…<能力>」或「新增<模块/能力>」句式，
        且 title/description 不得出现层级孤立标签。"""
        bad: List[str] = []
        for idx, c in enumerate(candidates):
            title = c.get("title") or ""
            if not self._TITLE_VERB_PATTERN.search(title):
                bad.append(f"第{idx+1}条 title《{title}》不符合「在X增加/改造Y」或「新增X」句式")
            full_text = f"{title} {c.get('description', '')}"
            for pat in self._LAYER_BAN_PATTERNS:
                if pat.search(full_text):
                    bad.append(f"第{idx+1}条出现层级孤立标签（运行时/租户级/流程级/节点级/跨层）")
                    break
        if bad:
            return False, "；".join(bad)
        # 目标模块重复度检查：允许至多 1 组重复（同模块不同改造粒度）
        modules = [(c.get("target_module") or "").strip() for c in candidates]
        non_empty = [m for m in modules if m]
        dup_count = len(non_empty) - len(set(non_empty))
        if dup_count > 1:
            return False, f"target_module 重复数过多（{dup_count} > 1），方案未对应不同模块"
        return True, ""

    def _build_solution_fallback(
        self,
        module_hint: str,
        core_problem: str,
        product_layer: str,
        problem_essence: str,
    ) -> List[Dict[str, Any]]:
        """LLM 三次尝试全部失败时的确定性降级：仍遵循「目标模块 + 改造动作」句式。"""
        primary_module = (module_hint or "相关模块").strip()
        essence = problem_essence or core_problem or "承接当前需求场景"
        return [
            {
                "title": f"在{primary_module}增强{essence[:20]}能力",
                "target_module": primary_module,
                "change_type": "现有模块增强",
                "source": "llm-degraded",
                "product_layer": product_layer,
                "description": f"在{primary_module}基础上补齐所需能力，作为最小可行改造点；具体入口与交互待产品细化。",
                "pros": ["改动面小", "复用现有架构"],
                "cons": ["可能无法一次覆盖所有场景，需后续迭代"],
                "applicable_when": "需求变更范围可控，现有架构可承接。",
            },
            {
                "title": f"新增独立模块承接{essence[:20]}",
                "target_module": "新增独立模块",
                "change_type": "新增独立能力",
                "source": "llm-degraded",
                "product_layer": product_layer,
                "description": f"为 {essence} 新建独立能力，不依赖现有模块，作为备选重方案。",
                "pros": ["设计自由度高", "不影响现有功能"],
                "cons": ["开发成本较高", "需考虑与现有系统集成"],
                "applicable_when": "现有架构无法承接，或需求本质上是全新能力。",
            },
        ]

    def _build_solution_candidates(
        self,
        req: Dict[str, Any],
        evidence_bundle: Dict[str, Any],
        related_requirements: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """返回包含 candidates / problem_essence / requirement_type / module_candidates 的完整方案包。

        注意：返回 shape 从 List 改为 Dict；调用方需取 dict["candidates"] 并把其他
        顶层字段回填到 artifact / analysis_packet。
        """
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        gap_analysis = ai_analysis.get("gap_analysis") or ""
        product_layer = ai_analysis.get("product_layer") or "待确认"
        current_behavior = ai_analysis.get("current_product_behavior") or ""
        module_hint = ai_analysis.get("module") or ""
        description = req.get("description", "")[:1500]

        modules_block = self._collect_module_candidates_for_prompt(evidence_bundle, module_hint)
        cases_block = self._collect_similar_cases_for_prompt(evidence_bundle, related_requirements)

        base_prompt = (
            "你是高级产品架构师。请严格按以下三步分析法为需求设计改造方案。\n\n"
            "【输入】\n"
            f"- 需求标题：{req.get('title', '')}\n"
            f"- 需求详情：{description}\n"
            f"- 核心问题：{core_problem}\n"
            f"- 现有产品行为：{current_behavior}\n"
            f"- 能力缺口：{gap_analysis}\n"
            f"- 所属模块提示：{module_hint}\n\n"
            "【现有产品模块候选（由系统从 KB 注入，你必须从中挑选或明确声明『新增 <模块名>』）】\n"
            f"{modules_block}\n\n"
            "【相似历史案例摘要】\n"
            f"{cases_block}\n\n"
            "【分析步骤】\n"
            "第一步 - 问题本质定位：\n"
            "  用一句 10-30 字的动宾结构概括用户真实诉求（不要复述标题，提炼动作+对象）。\n"
            "  归类 requirement_type ∈ {能力缺失, 交互优化, 配置扩展, 集成补齐, 治理规则, 性能, 其他}。\n"
            "第二步 - 现有产品框架盘点：\n"
            "  从候选清单中挑 3-5 个可承载该诉求的模块；每个写一句『为什么它可以承载』；\n"
            "  若候选清单均不合适，可加入『新增 <名称> 模块』，并在 why 中说明为何现有模块不适合。\n"
            "第三步 - 方案生成（3-4 条）：\n"
            "  每个方案 = 1 个目标模块 × 1 种改造动作；\n"
            "  title 必须是句式『在 <target_module> 增加/改造/扩展/增强/支持/补齐 <具体能力>』或『新增 <模块>』；\n"
            "  不同方案必须对应不同 target_module（最多允许 1 组『同模块不同改造粒度』）；\n"
            "  description 针对该方案独立撰写，不得出现相同或近似的共享段落。\n\n"
            "【硬性禁令】\n"
            "- 禁止用『运行时/租户级/流程级/节点级/跨层』作为方案标题或描述里的孤立标签（可作为模块名一部分）\n"
            "- 禁止把『能力缺口』或『核心问题』原文复述到每个方案的 description 里\n"
            "- 如需求未涉及『配置』，不要提配置方案；如未涉及『规则』，不要提规则方案\n\n"
            "【输出 JSON（严格 schema，不得添加额外字段）】\n"
            "{\n"
            '  "problem_essence": "一句动宾结构 10-30 字",\n'
            '  "requirement_type": "能力缺失|交互优化|配置扩展|集成补齐|治理规则|性能|其他",\n'
            '  "module_candidates": [{"name": "模块名", "why": "一句话承载理由"}],\n'
            '  "candidates": [\n'
            '    {"title": "...", "target_module": "...", "change_type": "...", "description": "80-200字具体做什么", "pros": ["..."], "cons": ["..."], "applicable_when": "..."}\n'
            "  ]\n"
            "}\n"
        )

        # T2+T5: 注入本轮审核意见 + 改动标注要求
        _cfg = getattr(self, "_current_llm_config", None) or {}
        _rev = (_cfg.get("revision_comments") or "").strip()
        _rev_iter = int(_cfg.get("revision_iteration") or 1)
        if _rev:
            base_prompt += (
                "\n【本轮审核意见（必须显式吸收）】\n"
                f"{_rev}\n"
                "请在 applicable_when / pros / description 等字段里点明已吸收哪条反馈。\n\n"
                "【本轮改动标注要求】\n"
                "对于基于以上意见修改过的方案，在该 candidate 对象里加上三个字段：\n"
                f'  "changed_in_iteration": {_rev_iter},\n'
                '  "linked_revision_comment": "<你吸收的意见片段，截取核心句>",\n'
                '  "llm_reasoning": "<一句话说明你为何这么改，与原版区别>"\n'
                "对于未改动的项可省略这三个字段。\n"
            )

        llm_config = getattr(self, "_current_llm_config", None)
        feedback_notes: List[str] = []
        last_result: Optional[Dict[str, Any]] = None

        for attempt in range(3):
            prompt = base_prompt
            if feedback_notes:
                prompt += "\n【上一轮反馈（必须修正）】\n" + "\n".join(f"- {note}" for note in feedback_notes) + "\n"

            llm_result = self._call_research_llm_json(prompt, llm_config)
            if not (llm_result and isinstance(llm_result, dict) and llm_result.get("candidates")):
                feedback_notes = ["上轮 LLM 返回为空或非合法 JSON，请严格按 schema 输出"]
                logger.warning("[ReqPoolDraft] solution LLM 第%s次无有效 JSON 返回", attempt + 1)
                continue

            raw_candidates = llm_result.get("candidates") or []
            candidates: List[Dict[str, Any]] = []
            for c in raw_candidates[:4]:
                if not isinstance(c, dict):
                    continue
                candidates.append(
                    {
                        "title": (c.get("title") or "").strip(),
                        "target_module": (c.get("target_module") or "").strip(),
                        "change_type": (c.get("change_type") or "").strip(),
                        "source": "llm-dynamic",
                        "product_layer": product_layer,
                        "description": (c.get("description") or "").strip(),
                        "pros": c.get("pros") or [],
                        "cons": c.get("cons") or [],
                        "applicable_when": (c.get("applicable_when") or "").strip(),
                    }
                )

            schema_issues: List[str] = []
            if len(candidates) < 3:
                schema_issues.append(f"方案数 {len(candidates)} < 3，请至少给出 3 条")
            if not (llm_result.get("problem_essence") or "").strip():
                schema_issues.append("缺少顶层 problem_essence 字段")
            if (llm_result.get("requirement_type") or "").strip() not in self._REQUIREMENT_TYPES:
                schema_issues.append(
                    "requirement_type 必须是 {能力缺失, 交互优化, 配置扩展, 集成补齐, 治理规则, 性能, 其他} 之一"
                )
            mc_raw = llm_result.get("module_candidates") or []
            if not isinstance(mc_raw, list) or len(mc_raw) < 3:
                schema_issues.append("module_candidates 数组必须有 ≥3 条，每条包含 name 和 why")
            for c in candidates:
                if not c["target_module"]:
                    schema_issues.append(f"方案《{c['title'] or '未命名'}》缺少 target_module 字段")
                    break
            if schema_issues:
                feedback_notes = schema_issues
                last_result = {"llm": llm_result, "candidates": candidates}
                logger.warning("[ReqPoolDraft] solution LLM 第%s次 schema 不合规：%s", attempt + 1, "; ".join(schema_issues))
                continue

            title_ok, title_msg = self._validate_title_format(candidates)
            if not title_ok:
                feedback_notes = [
                    title_msg,
                    "每条 title 必须形如「在<目标模块>增加/改造/扩展 <能力>」或「新增<模块/能力>」，并避开层级孤立标签",
                ]
                last_result = {"llm": llm_result, "candidates": candidates}
                logger.warning("[ReqPoolDraft] solution LLM 第%s次 title 校验失败：%s", attempt + 1, title_msg)
                continue

            div_ok, div_msg = self._validate_solution_diversity(candidates)
            if not div_ok:
                feedback_notes = [
                    div_msg,
                    "请让每个方案对应不同目标模块，或至少让 description 从改造动作、影响面、成本维度显著区分",
                ]
                last_result = {"llm": llm_result, "candidates": candidates}
                logger.warning("[ReqPoolDraft] solution LLM 第%s次 多样性校验失败：%s", attempt + 1, div_msg)
                continue

            module_candidates_out = []
            for mc in mc_raw[:8]:
                if isinstance(mc, dict) and mc.get("name"):
                    module_candidates_out.append(
                        {"name": str(mc["name"]).strip(), "why": str(mc.get("why") or "").strip()}
                    )

            return {
                "candidates": candidates,
                "problem_essence": (llm_result.get("problem_essence") or "").strip(),
                "requirement_type": (llm_result.get("requirement_type") or "其他").strip(),
                "module_candidates": module_candidates_out,
                "generation_status": "llm-ok",
            }

        # 三次尝试均失败 → 降级
        logger.warning("[ReqPoolDraft] solution LLM 三次尝试均失败，启用降级方案；最近一次反馈：%s", feedback_notes)
        problem_essence_fallback = ""
        if last_result and isinstance(last_result.get("llm"), dict):
            problem_essence_fallback = (last_result["llm"].get("problem_essence") or "").strip()
        degraded = self._build_solution_fallback(module_hint, core_problem, product_layer, problem_essence_fallback)
        return {
            "candidates": degraded,
            "problem_essence": problem_essence_fallback or core_problem or "",
            "requirement_type": "其他",
            "module_candidates": [
                {"name": module_hint or "相关模块", "why": "module_hint 兜底"},
                {"name": "新增独立模块", "why": "当现有模块不足以承接时的备选"},
            ],
            "generation_status": "llm-degraded",
        }

    def _dedupe_evidence_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduped: List[Dict[str, Any]] = []
        seen = set()
        for item in items:
            key = item.get("content_id") or item.get("citation_label") or item.get("name")
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        deduped.sort(
            key=lambda item: (
                item.get("source_kind") == "ticket_case",
                -float(item.get("weighted_rank_score", item.get("base_relevance_score", item.get("score", 0.0))) or 0.0),
                item.get("name", ""),
            )
        )
        return deduped

    def _collect_capability_evidence(
        self,
        req: Dict[str, Any],
        module_hint: str,
        evidence_bundle: Dict[str, Any],
        ticket_appendix: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        collected = list(evidence_bundle.get("primary_materials", []) or [])
        collected.extend(item for item in (evidence_bundle.get("evidence", []) or []) if item.get("source_kind") != "ticket_case")
        collected.extend(self._manifest_capability_hits(req, module_hint, evidence_bundle))
        return self._dedupe_evidence_items(collected)[:12]

    def _formal_evidence_count(self, evidence_bundle: Dict[str, Any]) -> int:
        explicit_count = evidence_bundle.get("primary_material_count")
        if explicit_count is not None:
            return int(explicit_count or 0)
        seen = set()
        for item in (evidence_bundle.get("primary_materials", []) or []) + (evidence_bundle.get("evidence", []) or []):
            if item.get("source_kind") == "ticket_case":
                continue
            key = item.get("content_id") or item.get("citation_label") or item.get("name")
            if key:
                seen.add(key)
        return len(seen)

    def _design_fact_entries_as_evidence(self, design_fact_bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
        evidence: List[Dict[str, Any]] = []
        for category in ("process_rules", "step_rules", "tenant_params", "document_properties"):
            for item in design_fact_bundle.get(category, []) or []:
                evidence.append(
                    {
                        "name": item.get("name", "未命名设计事实"),
                        "summary": item.get("summary", ""),
                        "source_kind": item.get("source", "design_fact"),
                        "citation_label": item.get("citation_label", "[FACT] 产品设计事实"),
                        "weighted_rank_score": 97,
                        "base_relevance_score": 92,
                        "relevance_level": "high",
                    }
                )
        return evidence

    def _build_business_scenarios(self, req: Dict[str, Any], ticket_appendix: List[Dict[str, Any]], capability_evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        focus_tickets = self._select_focus_tickets(ticket_appendix, req)
        ticket_lines = [
            f"- {t.get('issue_key','')} | {t.get('customer_type','')} | {(t.get('summary') or '')[:80]}"
            for t in focus_tickets[:5]
        ]
        cap_hints = [item.get("name", "") for item in capability_evidence[:4] if item.get("name")]

        prompt = (
            "你是高级产品经理。为以下需求拆解 2-4 个真实业务场景。\n\n"
            f"【需求标题】{req.get('title', '')}\n"
            f"【核心问题】{core_problem}\n"
            "【相关工单（可作为场景素材）】\n"
            + ("\n".join(ticket_lines) or "- 暂无典型工单") + "\n\n"
            f"【相关能力提示】{', '.join(cap_hints) or '暂无'}\n\n"
            "【分析步骤】\n"
            "第一步 - 从工单的客户类型和摘要中识别 2-4 个差异化真实场景（不要用同一场景的不同描述凑数）\n"
            "第二步 - 每个场景给出 actors（参与角色）、trigger（触发条件）、summary（场景描述）、expected_value（期望价值）\n"
            "第三步 - 每个场景的 evidence_ticket_ids 列出对应工单号\n\n"
            "【硬性禁令】\n"
            "- 禁止所有场景都用『支持人员/实施顾问/产品经理』三种泛化角色作为 actors\n"
            "- 每个 title 必须与需求本质直接相关，不得出现『能力边界核实』这类元场景\n\n"
            '请只输出合法 JSON：\n'
            '{"scenarios": [{"title": "场景标题", "actors": ["角色1"], "trigger": "触发条件", "summary": "场景描述", "expected_value": "期望价值", "evidence_ticket_ids": ["TICKET-001"]}]}'
        )

        _cfg = getattr(self, "_current_llm_config", None) or {}
        _rev = (_cfg.get("revision_comments") or "").strip()
        _rev_iter = int(_cfg.get("revision_iteration") or 1)
        if _rev:
            prompt += (
                "\n【本轮审核意见（必须显式吸收）】\n"
                f"{_rev}\n"
                "请在场景中体现意见修正；对于本轮新增或修改的场景，加上：\n"
                f'  "changed_in_iteration": {_rev_iter}, "linked_revision_comment": "<意见片段>", "llm_reasoning": "<改动原因>"\n'
            )

        llm_result = self._call_research_llm_json(prompt, getattr(self, "_current_llm_config", None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("scenarios"):
            scenarios = []
            for s in llm_result["scenarios"][:4]:
                if not isinstance(s, dict) or not s.get("title"):
                    continue
                entry: Dict[str, Any] = {
                    "title": s["title"].strip(),
                    "actors": s.get("actors") or [],
                    "trigger": (s.get("trigger") or "").strip(),
                    "summary": (s.get("summary") or "").strip(),
                    "expected_value": (s.get("expected_value") or "").strip(),
                    "citations": s.get("evidence_ticket_ids") or [],
                    "source": "llm-ok",
                }
                if s.get("changed_in_iteration"):
                    entry["changed_in_iteration"] = s["changed_in_iteration"]
                    entry["linked_revision_comment"] = s.get("linked_revision_comment", "")
                    entry["llm_reasoning"] = s.get("llm_reasoning", "")
                scenarios.append(entry)
            if len(scenarios) >= 2:
                return scenarios

        return self._build_business_scenarios_fallback(req, ticket_appendix, capability_evidence)

    def _build_business_scenarios_fallback(self, req: Dict[str, Any], ticket_appendix: List[Dict[str, Any]], capability_evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        focus = self._extract_requirement_focus(req)
        ai_analysis = req.get("ai_analysis", {}) or {}
        title = req.get("title", "")
        title_hint = ai_analysis.get("core_problem") or focus.get("summary") or title or "当前需求"
        primary_surface = (focus.get("surfaces") or ["当前页面"])[0]
        primary_action = (focus.get("actions") or ["处理"])[0]
        primary_object = (focus.get("objects") or focus.get("scenario_keywords") or ["当前信息"])[0]
        citations = [item.get("citation_label", "") for item in capability_evidence[:5] if item.get("citation_label")]
        focus_tickets = self._select_focus_tickets(ticket_appendix, req)
        ticket_refs = [item.get("issue_key", "") for item in focus_tickets if item.get("issue_key")]
        actor_pool = [item.get("customer_type", "") for item in focus_tickets if item.get("customer_type")]
        actors = self._dedupe_terms(actor_pool)[:3] or ["支持人员", "实施顾问", "产品经理"]
        product_layer = focus.get("product_layer") or ai_analysis.get("product_layer") or "待确认"
        gap_hint = ai_analysis.get("gap_analysis") or ai_analysis.get("root_cause") or "现有能力尚未稳定覆盖该场景"
        return [
            {
                "title": f"{primary_surface}{primary_action}{primary_object}",
                "actors": actors,
                "summary": f"用户希望在 {primary_surface} 执行 {primary_action} 时，系统能直接承接『{title_hint}』，而不是依赖线下沟通或额外补救。",
                "trigger": f"当用户进入 {primary_surface} 并触发 {primary_action} 动作时触发。",
                "expected_value": f"让 {primary_object} 的处理过程更可控，避免因 {gap_hint} 导致返工或遗漏。",
                "citations": list(dict.fromkeys(ticket_refs + citations))[:5],
                "source": "llm-degraded",
            },
            {
                "title": f"{product_layer}能力承接与规则落位",
                "actors": actors[:2] or ["流程管理员", "产品经理"],
                "summary": f"该需求不仅是一次页面补丁，还要明确能力应该落在 {product_layer} 还是其他层级，避免后续类似场景继续重复提单。",
                "trigger": "当同类问题会在多个流程、多个节点或多个客户环境中重复出现时触发。",
                "expected_value": "把能力落位和配置边界讲清楚，让方案既能快速上线，也能支撑后续治理和扩展。",
                "citations": citations[:5],
                "source": "llm-degraded",
            },
        ]

    def _build_capability_analysis(self, req: Dict[str, Any], capability_evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        gap_analysis = ai_analysis.get("gap_analysis") or ""
        cap_summaries = [
            f"{item.get('name', '')}: {(item.get('summary') or '')[:100]}"
            for item in capability_evidence[:6]
            if item.get("name")
        ]

        prompt = (
            "你是高级产品架构师。分析以下需求相关的现有能力与缺口。\n\n"
            f"【需求标题】{req.get('title', '')}\n"
            f"【核心问题】{core_problem}\n"
            f"【能力缺口线索】{gap_analysis}\n"
            "【相关能力资料】\n"
            + ("\n".join(f"- {s}" for s in cap_summaries) or "- 暂无能力资料") + "\n\n"
            "【分析步骤】\n"
            "第一步 - 列出该需求场景下相关的现有能力，评估每项对需求的覆盖度（full/partial/missing）\n"
            "第二步 - 识别主要能力缺口：现有能力覆盖不到的具体功能点或场景边界\n"
            "第三步 - 输出结构化结果\n\n"
            "【硬性禁令】\n"
            "- 禁止使用『暂无』『待补充』『需确认』这类泛词填充 coverage_gaps\n"
            "- coverage_gaps 必须针对该需求的具体缺失，不得仅复述能力列表\n\n"
            '请只输出合法 JSON：\n'
            '{"current_capabilities": [{"title": "能力名", "summary": "一句话说明", "coverage": "full或partial或missing", "citation": "来源"}], '
            '"coverage_gaps": ["缺口描述1", "缺口描述2"]}'
        )

        _cfg = getattr(self, "_current_llm_config", None) or {}
        _rev = (_cfg.get("revision_comments") or "").strip()
        _rev_iter = int(_cfg.get("revision_iteration") or 1)
        if _rev:
            prompt += (
                "\n【本轮审核意见（必须显式吸收）】\n"
                f"{_rev}\n"
                "请在能力评估中体现意见修正；对于本轮新增或修改的项，加上：\n"
                f'  "changed_in_iteration": {_rev_iter}, "linked_revision_comment": "<意见片段>", "llm_reasoning": "<改动原因>"\n'
            )

        llm_result = self._call_research_llm_json(prompt, getattr(self, "_current_llm_config", None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("current_capabilities"):
            caps = []
            for c in llm_result["current_capabilities"][:6]:
                if not isinstance(c, dict) or not c.get("title"):
                    continue
                entry: Dict[str, Any] = {
                    "title": c["title"].strip(),
                    "summary": (c.get("summary") or "").strip(),
                    "coverage": c.get("coverage", "partial"),
                    "citation": c.get("citation", ""),
                    "source_kind": "llm-analyzed",
                }
                if c.get("changed_in_iteration"):
                    entry["changed_in_iteration"] = c["changed_in_iteration"]
                    entry["linked_revision_comment"] = c.get("linked_revision_comment", "")
                    entry["llm_reasoning"] = c.get("llm_reasoning", "")
                caps.append(entry)
            gaps = [g for g in (llm_result.get("coverage_gaps") or []) if g and isinstance(g, str)]
            if caps:
                return {
                    "current_capabilities": caps,
                    "coverage_gaps": gaps[:4],
                    "coverage": {},
                    "source": "llm-ok",
                    "generation_status": "llm-ok",
                }

        return self._build_capability_analysis_fallback(req, capability_evidence)

    def _build_capability_analysis_fallback(self, req: Dict[str, Any], capability_evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
        current_capabilities = [
            {
                "title": item.get("name", "未命名能力"),
                "summary": item.get("summary", ""),
                "source_kind": item.get("source_kind", ""),
                "citation": item.get("citation_label", ""),
            }
            for item in capability_evidence[:6]
        ]
        capability_text = " ".join(c.get("title","") + " " + c.get("summary","") for c in current_capabilities)
        focus = self._extract_requirement_focus(req)
        coverage_gaps = []
        if not current_capabilities:
            coverage_gaps.append("缺少和当前需求点直接相关的现有能力资料，无法确认复用边界。")
        core_problem = (req.get("ai_analysis", {}) or {}).get("core_problem") or req.get("title", "当前需求")
        for term in (focus.get("objects") or [])[:2]:
            if term not in capability_text:
                coverage_gaps.append(f"现有资料未直接说明 {term} 与「{core_problem}」相关的能力边界。")
        for surface in (focus.get("surfaces") or [])[:2]:
            if surface not in capability_text:
                coverage_gaps.append(f"{surface} 相关资料不足，需确认是否承接当前需求场景。")
        return {
            "current_capabilities": current_capabilities,
            "coverage_gaps": self._dedupe_terms(coverage_gaps),
            "coverage": {},
            "source": "llm-degraded",
            "generation_status": "llm-degraded",
        }

    def _build_upstream_downstream_analysis(self, req: Dict[str, Any], capability_evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        capability_names = [item.get("name", "") for item in capability_evidence[:5] if item.get("name")]

        prompt = (
            "你是高级���品架构师。分析以下需求的上下游依赖关系。\n\n"
            f"【需求标题���{req.get('title', '')}\n"
            f"【核心问题】{core_problem}\n"
            f"【现有能力证据】{', '.join(capability_names) if capability_names else '暂无'}\n\n"
            "要求：\n"
            "1. 只列出与该需求直接相关的上下游依赖（不要套用通用框架）\n"
            "2. upstream = 该需求��赖的前置能力/配置/���据\n"
            "3. downstream = 该需求完成后影响的后续环节/展示/流程\n"
            "4. 如果某个方向没有明确依赖，不要硬凑\n\n"
            '请只输出合法 JSON：\n'
            '{"analysis": [\n'
            '  {"surface": "依赖面名称", "connection": "upstream或downstream或reuse", "summary": "一句话说明"}\n'
            ']}'
        )
        # T2+T5: 注入本轮审核意见
        _cfg = getattr(self, "_current_llm_config", None) or {}
        _rev = (_cfg.get("revision_comments") or "").strip()
        _rev_iter = int(_cfg.get("revision_iteration") or 1)
        if _rev:
            prompt += (
                "\n【本轮审核意见（必须显式吸收）】\n"
                f"{_rev}\n"
                "请在 analysis 项中体现意见修正；对于本轮新增或修改的项，加上：\n"
                f'  "changed_in_iteration": {_rev_iter}, '
                '"linked_revision_comment": "<意见片段>", "llm_reasoning": "<改动原因>"\n'
            )
        llm_result = self._call_research_llm_json(prompt, getattr(self, '_current_llm_config', None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("analysis"):
            return llm_result["analysis"][:5]

        # 降级：仅基于现有证据
        fallback = []
        if capability_names:
            fallback.append({"surface": "现有能力证据", "connection": "reuse",
                "summary": f"已有 {len(capability_names)} 条相关能力记录，需确认哪些可复用。"})
        return fallback

    def _build_upstream_downstream_analysis_LEGACY(self, req, capability_evidence):
        """旧硬编码版本，保留备用"""
        analysis = []
        joined_text = " ".join(f"{item.get('name', '')} {item.get('summary', '')}" for item in capability_evidence)
        focus = self._extract_requirement_focus(req)
        product_layer = focus.get("product_layer") or (req.get("ai_analysis", {}) or {}).get("product_layer") or "待确认"
        surfaces = focus.get("surfaces", []) or []
        scenario_keywords = focus.get("scenario_keywords", []) or []

        if product_layer in {"运行时", "跨层"}:
            analysis.append(
                {
                    "surface": surfaces[0] if surfaces else "运行时入口",
                    "connection": "upstream",
                    "summary": "当前需求依赖用户在运行时页面或操作入口发起动作，需确认入口位置、触发条件和提示文案。",
                }
            )
        if product_layer in {"流程级", "节点级", "跨层"}:
            analysis.append(
                {
                    "surface": "流程/节点配置",
                    "connection": "upstream",
                    "summary": "需要确认流程属性或节点属性如何决定该能力是否生效，以及运行时如何解释这些配置。",
                }
            )
        if product_layer in {"租户级", "跨层"}:
            analysis.append(
                {
                    "surface": "租户级配置",
                    "connection": "upstream",
                    "summary": "若能力需要面向多个流程统一治理，需确认租户级开关、默认值及灰度策略。",
                }
            )
        if surfaces:
            analysis.append(
                {
                    "surface": surfaces[0],
                    "connection": "downstream",
                    "summary": f"{surfaces[0]} 是当前需求的主要承接面，需明确 {focus.get('summary') or '该场景'} 的展示、校验或说明方式。",
                }
            )
        if scenario_keywords:
            analysis.append(
                {
                    "surface": "关联能力/相似场景",
                    "connection": "downstream",
                    "summary": f"需结合 { '、'.join(scenario_keywords[:4]) } 等场景关键词核实后续能力复用、留痕位置和数据可见边界。",
                }
            )
        if joined_text:
            analysis.append(
                {
                    "surface": "现有能力证据",
                    "connection": "reuse",
                    "summary": "已命中的知识库/设计事实可作为上下游边界判定依据，优先确认哪些能力是复用、扩展还是新增。",
                }
            )
        return analysis[:5]

    def _build_change_impact(self, req: Dict[str, Any], capability_analysis: Dict[str, Any], upstream_downstream_analysis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        gap_analysis = ai_analysis.get("gap_analysis") or ""
        upstream_summary = "; ".join(item.get("summary", "")[:50] for item in upstream_downstream_analysis[:3])

        prompt = (
            "你是高级产品架构师。分析以下需求的功能改动点和影响面。\n\n"
            f"【需求标题】{req.get('title', '')}\n"
            f"【核心问题】{core_problem}\n"
            f"【能力缺口】{gap_analysis}\n"
            f"【上下游依赖】{upstream_summary or '暂无'}\n\n"
            "要求：\n"
            "1. 只列出与该需求直接相关的改动点（不要套用通用层级框架）\n"
            "2. 每个改动点说明具体改什么、为什么改\n"
            "3. change_type: enhance(增强)/clarify(澄清)/new(新增)\n\n"
            '请只输出合法 JSON：\n'
            '{"impacts": [\n'
            '  {"surface": "改动面名称", "change_type": "enhance", "summary": "具体改动说明"}\n'
            ']}'
        )
        # T2+T5: 注入本轮审核意见
        _cfg = getattr(self, "_current_llm_config", None) or {}
        _rev = (_cfg.get("revision_comments") or "").strip()
        _rev_iter = int(_cfg.get("revision_iteration") or 1)
        if _rev:
            prompt += (
                "\n【本轮审核意见（必须显式吸收）】\n"
                f"{_rev}\n"
                "请在 impacts 项中体现意见修正；对于本轮新增或修改的项，加上：\n"
                f'  "changed_in_iteration": {_rev_iter}, '
                '"linked_revision_comment": "<意见片段>", "llm_reasoning": "<改动原因>"\n'
            )
        llm_result = self._call_research_llm_json(prompt, getattr(self, '_current_llm_config', None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("impacts"):
            return llm_result["impacts"][:6]

        # 降级到原有逻辑
        focus = self._extract_requirement_focus(req)
        ai_analysis = req.get("ai_analysis", {}) or {}
        impacts: List[Dict[str, Any]] = []
        product_layer = focus.get("product_layer") or ai_analysis.get("product_layer") or "待确认"
        core_problem = ai_analysis.get("core_problem") or focus.get("summary") or "当前需求"
        gap_analysis = ai_analysis.get("gap_analysis") or ai_analysis.get("root_cause") or "现有能力覆盖不足"
        surfaces = focus.get("surfaces") or []
        scenario_keywords = focus.get("scenario_keywords") or []

        layer_impacts = {
            "运行时": [
                {
                    "surface": surfaces[0] if surfaces else "运行时页面",
                    "change_type": "enhance",
                    "summary": f"需在运行时触点补充 {core_problem} 的入口、提示、结果回显或交互联动。",
                }
            ],
            "租户级": [
                {
                    "surface": "租户级配置",
                    "change_type": "enhance",
                    "summary": "需增加租户级启用范围、默认值或灰度控制，避免每个流程重复单独配置。",
                }
            ],
            "流程级": [
                {
                    "surface": "流程属性/流程设计器",
                    "change_type": "enhance",
                    "summary": "需在流程级补充属性配置和解释逻辑，让不同流程可独立控制能力边界。",
                }
            ],
            "节点级": [
                {
                    "surface": "节点/环节属性",
                    "change_type": "enhance",
                    "summary": "需在节点级补充属性配置，使能力只作用于指定审批环节或签署环节。",
                }
            ],
            "跨层": [
                {
                    "surface": "跨层联动",
                    "change_type": "enhance",
                    "summary": "需同时梳理运行时交互、配置层控制和节点/流程规则，避免只改单点导致链路断裂。",
                }
            ],
        }
        impacts.extend(layer_impacts.get(product_layer, []))

        for surface in surfaces[:3]:
            if surface in {item.get("surface") for item in impacts}:
                continue
            impacts.append(
                {
                    "surface": surface,
                    "change_type": "enhance",
                    "summary": f"需在 {surface} 补充 {core_problem} 的触发入口、提示说明或结果呈现。",
                }
            )

        if scenario_keywords:
            impacts.append(
                {
                    "surface": "检索与规则匹配",
                    "change_type": "clarify",
                    "summary": f"需让工单召回、能力分析或规则判断优先围绕 { '、'.join(scenario_keywords[:4]) } 等场景关键词展开，降低历史硬编码偏差。",
                }
            )
        if gap_analysis:
            impacts.append(
                {
                    "surface": "能力边界说明",
                    "change_type": "clarify",
                    "summary": f"需明确现有能力与新增能力的边界，当前主要缺口为：{gap_analysis}",
                }
            )
        if any("权限" in gap for gap in capability_analysis.get("coverage_gaps", [])):
            impacts.append(
                {
                    "surface": "权限边界",
                    "change_type": "clarify",
                    "summary": "需明确不同角色查看、配置或触发该能力的可见范围。",
                }
            )
        for item in upstream_downstream_analysis:
            if item.get("surface") not in {impact.get("surface") for impact in impacts}:
                impacts.append(
                    {
                        "surface": item.get("surface", "上下游能力"),
                        "change_type": "reuse" if item.get("connection") in {"upstream", "reuse"} else "enhance",
                        "summary": item.get("summary", ""),
                    }
                )
        return impacts[:6]

    def _build_functional_architecture(self, req: Dict[str, Any], change_impact: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        impact_summary = "; ".join(
            f"{item.get('surface','')}: {(item.get('summary') or '')[:60]}"
            for item in change_impact[:4]
        )

        prompt = (
            "你是高级产品架构师。为以下需求的功能改造方案生成功能架构组件清单。\n\n"
            f"【需求标题】{req.get('title', '')}\n"
            f"【核心问题】{core_problem}\n"
            f"【改动面摘要】{impact_summary or '暂无'}\n\n"
            "【分析步骤】\n"
            "第一步 - 识别本需求的核心能力链路（输入→处理→输出）\n"
            "第二步 - 拆出最多 5 个功能组件，每个组件对应链路上的一个职责\n"
            "第三步 - 每个组件给 component（组件名）和 responsibility（职责一句话）\n\n"
            "【硬性禁令】\n"
            "- 禁止用『交互触发层/规则判断层』这类与本需求无关的通用层级标签\n"
            "- 每个 component 名必须与本需求的实际功能点直接相关\n\n"
            '请只输出合法 JSON：\n'
            '{"components": [{"component": "组件名", "responsibility": "职责说明"}]}'
        )

        _cfg = getattr(self, "_current_llm_config", None) or {}
        _rev = (_cfg.get("revision_comments") or "").strip()
        _rev_iter = int(_cfg.get("revision_iteration") or 1)
        if _rev:
            prompt += (
                "\n【本轮审核意见（必须显式吸收）】\n"
                f"{_rev}\n"
                "请在架构组件中体现意见修正；对于本轮新增或修改的项，加上：\n"
                f'  "changed_in_iteration": {_rev_iter}, "linked_revision_comment": "<意见片段>", "llm_reasoning": "<改动原因>"\n'
            )

        llm_result = self._call_research_llm_json(prompt, getattr(self, "_current_llm_config", None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("components"):
            comps = []
            for c in llm_result["components"][:5]:
                if not isinstance(c, dict) or not c.get("component"):
                    continue
                entry: Dict[str, Any] = {
                    "component": c["component"].strip(),
                    "responsibility": (c.get("responsibility") or "").strip(),
                }
                if c.get("changed_in_iteration"):
                    entry["changed_in_iteration"] = c["changed_in_iteration"]
                    entry["linked_revision_comment"] = c.get("linked_revision_comment", "")
                    entry["llm_reasoning"] = c.get("llm_reasoning", "")
                comps.append(entry)
            if comps:
                return comps

        return self._build_functional_architecture_fallback(req, change_impact)

    def _build_functional_architecture_fallback(self, req: Dict[str, Any], change_impact: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        focus = self._extract_requirement_focus(req)
        architecture = []
        surfaces = {item.get("surface") for item in change_impact}
        if any(s in surfaces for s in ["审批面板", "重提弹窗", "流程监控"]):
            architecture.append({"component": "交互触发层", "responsibility": "承接当前页面上的重提、补充说明或展示交互。"})
        if any(term in (focus.get("objects") or []) for term in ["附言", "审批人"]):
            architecture.append({"component": "输入与校验层", "responsibility": "处理说明录入、必填校验、默认值和字段回填。"})
        if "输入与校验规则" in surfaces:
            architecture.append({"component": "规则判断层", "responsibility": "根据触发动作、页面状态和业务规则决定是否拦截或提示。"})
        if "审批面板" in surfaces:
            architecture.append({"component": "审批面板展示层", "responsibility": "展示补充说明、审批意见或相关提示信息。"})
        if "审批意见/留痕" in surfaces:
            architecture.append({"component": "说明透传与留痕层", "responsibility": "把补充说明同步到后续审批节点和留痕记录。"})
        return architecture

    def _build_source_citations(self, ticket_appendix: List[Dict[str, Any]], capability_evidence: List[Dict[str, Any]]) -> List[str]:
        citations = [f"[TICKET] {item.get('issue_key', '')}" for item in ticket_appendix if item.get("issue_key")]
        citations.extend(item.get("citation_label", "") for item in capability_evidence if item.get("citation_label"))
        return list(dict.fromkeys([item for item in citations if item]))[:20]

    def _build_risk_summary(self, ticket_summary: Dict[str, Any], evidence_bundle: Dict[str, Any], req: Dict[str, Any]) -> List[str]:
        ai_analysis = req.get("ai_analysis", {}) or {}
        core_problem = ai_analysis.get("core_problem") or req.get("title", "")
        gap_analysis = ai_analysis.get("gap_analysis") or ""
        top_modules = ticket_summary.get("top_modules", []) or []
        matched_count = evidence_bundle.get("matched_count", 0)

        prompt = (
            "你是产品风险分析师。为以下需求识别 3-5 个具体风险点。\n\n"
            f"【需求标题】{req.get('title', '')}\n"
            f"【核心问题】{core_problem}\n"
            f"【能力缺口】{gap_analysis}\n"
            f"【涉及模块】{', '.join(str(m) for m in top_modules[:4]) or '待确认'}\n"
            f"【知识证据数量】{matched_count}\n\n"
            "要求：\n"
            "1. 每条风险点针对本需求的具体特征，不要套用通用风险模板\n"
            "2. 每条描述 20-60 字，有可操作的缓解方向\n"
            "3. 输出 3-5 条，按严重程度降序\n\n"
            '请只输出合法 JSON：\n'
            '{"risks": ["风险描述1", "风险描述2"]}'
        )
        _cfg = getattr(self, "_current_llm_config", None) or {}
        _rev = (_cfg.get("revision_comments") or "").strip()
        if _rev:
            prompt += f"\n【本轮审核意见（必须显式吸收）】\n{_rev}\n"

        llm_result = self._call_research_llm_json(prompt, getattr(self, "_current_llm_config", None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("risks"):
            risks = [r for r in llm_result["risks"] if isinstance(r, str) and r.strip()]
            if risks:
                return risks[:5]

        # 降级
        risks = []
        if len(top_modules) > 1:
            risks.append("跨模块联动较多，存在边界定义和联调风险。")
        if matched_count == 0:
            risks.append("外部知识证据不足，当前方案判断存在偏差风险。")
        if "查询" in req.get("title", "") or "查询" in req.get("description", ""):
            risks.append("查询口径、权限范围和性能约束需要提前澄清。")
        if not risks:
            risks.append("当前风险集中在需求边界澄清和上线前回归验证。")
        return risks[:4]

    def _estimate_effort_size(self, ticket_summary: Dict[str, Any], evidence_bundle: Dict[str, Any], related_requirements: List[Dict[str, Any]]) -> str:
        module_count = len(ticket_summary.get("top_modules", []))
        evidence_count = evidence_bundle.get("matched_count", 0)
        related_count = len(related_requirements)
        ticket_count = ticket_summary.get("matched_ticket_count", 0)
        if module_count >= 4 or evidence_count >= 10 or related_count >= 5 or ticket_count >= 80:
            return "XL"
        if module_count >= 3 or evidence_count >= 6 or related_count >= 3 or ticket_count >= 30:
            return "L"
        if module_count >= 2 or evidence_count >= 3 or ticket_count >= 10:
            return "M"
        return "S"

    def _build_pending_questions(self, evidence_bundle: Dict[str, Any], risks: List[str]) -> List[str]:
        open_questions = list(evidence_bundle.get("open_questions", []) or [])
        if len(open_questions) >= 3:
            return list(dict.fromkeys(open_questions))[:6]

        risk_text = " ".join(risks[:3])
        prompt = (
            "根据以下风险点，提炼 2-4 个需要人工拍板的关键开放问题。\n\n"
            f"【风险点】{risk_text}\n"
            f"【已有开放问题】{open_questions or '无'}\n\n"
            "要求：每条问题对应一个具体决策点，不要泛化，不要重复。\n"
            '请只输出合法 JSON：\n'
            '{"questions": ["问题1", "问题2"]}'
        )
        llm_result = self._call_research_llm_json(prompt, getattr(self, "_current_llm_config", None))
        if llm_result and isinstance(llm_result, dict) and llm_result.get("questions"):
            qs = [q for q in llm_result["questions"] if isinstance(q, str) and q.strip()]
            merged = list(dict.fromkeys(open_questions + qs))
            if merged:
                return merged[:6]

        # 降级
        questions = list(open_questions)
        if any("权限" in risk or "查询口径" in risk for risk in risks):
            questions.append("需人工确认查询口径、权限范围与展示边界。")
        questions.append("若涉及架构方向调整，需产品/架构人工拍板。")
        return list(dict.fromkeys(questions))[:6]

    def _run_researcher(self, req: Dict[str, Any], plan: Dict[str, Any], llm_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._current_llm_config = llm_config  # 供 _build_* 方法调用 LLM
        module_hint = plan.get("module_hint") or ""
        query_text = f"{req.get('title', '')}\n{req.get('description', '')[:1500]}".strip()

        evidence_bundle = req.get("ai_analysis", {}).get("evidence_bundle", {}) or {}
        kb_llm_config = dict(llm_config or {})
        if self.kb_runtime_service:
            try:
                evidence_bundle = self.kb_runtime_service.analyze(
                    summary=query_text,
                    module_hint=module_hint,
                    top_k=10,
                    llm_config=kb_llm_config,
                )
            except Exception:
                evidence_bundle = evidence_bundle or {}

        raw_ticket_appendix = self._build_ticket_appendix(req)
        ticket_appendix, ticket_filter_stats = self._apply_ticket_soft_filter(raw_ticket_appendix, req)
        promoted_source_issues = self._bootstrap_source_issues_from_matches(req, ticket_appendix)
        if promoted_source_issues:
            self._persist_bootstrapped_source_issues(req, promoted_source_issues)
            ticket_filter_stats["source_issue_count"] = max(
                int(ticket_filter_stats.get("source_issue_count", 0) or 0),
                len(promoted_source_issues),
            )
        ticket_summary = self._build_ticket_summary(ticket_appendix, ticket_filter_stats)
        formal_evidence_count = self._formal_evidence_count(evidence_bundle)
        knowledge_context = (
            self.design_fact_service.build_requirement_context(
                {**req, "ai_analysis": {**(req.get("ai_analysis", {}) or {}), "evidence_bundle": evidence_bundle}},
                evidence_bundle=evidence_bundle,
            )
            if self.design_fact_service
            else {
                "fact_packet": req.get("requirement_fact_packet", {}) or {},
                "design_fact_bundle": {
                    "design_principles": [],
                    "process_rules": [],
                    "step_rules": [],
                    "tenant_params": [],
                    "document_properties": [],
                    "coverage_summary": {"total_fact_count": 0, "missing_fact_count": 0},
                    "missing_facts": [],
                    "source_refs": [],
                },
                "competitor_dossiers": [],
            }
        )
        design_fact_bundle = knowledge_context.get("design_fact_bundle", {}) or {}
        competitor_dossiers = knowledge_context.get("competitor_dossiers", []) or []
        customer_profiles = self._build_customer_profiles(req, ticket_appendix)
        related_requirements = self._build_related_requirements(req)
        capability_evidence = self._collect_capability_evidence(req, module_hint, evidence_bundle, ticket_appendix)
        capability_evidence = self._dedupe_evidence_items(self._design_fact_entries_as_evidence(design_fact_bundle) + capability_evidence)
        business_scenarios = self._build_business_scenarios(req, ticket_appendix, capability_evidence)
        capability_analysis = self._build_capability_analysis(req, capability_evidence)
        upstream_downstream_analysis = self._build_upstream_downstream_analysis(req, capability_evidence)
        change_impact = self._build_change_impact(req, capability_analysis, upstream_downstream_analysis)
        functional_architecture = self._build_functional_architecture(req, change_impact)
        risk_summary = self._build_risk_summary(ticket_summary, evidence_bundle, req)
        effort_estimate_size = self._estimate_effort_size(ticket_summary, evidence_bundle, related_requirements)
        solution_bundle = self._build_solution_candidates(req, evidence_bundle, related_requirements)
        solution_candidates = solution_bundle.get("candidates") or []
        problem_essence = solution_bundle.get("problem_essence", "")
        requirement_type = solution_bundle.get("requirement_type", "其他")
        module_candidates = solution_bundle.get("module_candidates") or []
        solution_generation_status = solution_bundle.get("generation_status", "")
        pending_questions = self._build_pending_questions(evidence_bundle, risk_summary)
        pending_questions = list(dict.fromkeys((pending_questions or []) + (design_fact_bundle.get("missing_facts", []) or [])))
        source_citations = self._build_source_citations(ticket_appendix, capability_evidence)
        source_citations = list(dict.fromkeys(source_citations + (design_fact_bundle.get("source_refs", []) or [])))
        analysis_summary = self._build_analysis_summary(
            req,
            {
                "module_hint": module_hint,
                "evidence_bundle": evidence_bundle,
                "ticket_summary": ticket_summary,
                "customer_profiles": customer_profiles,
                "business_scenarios": business_scenarios,
                "capability_analysis": capability_analysis,
                "risk_summary": risk_summary,
                "design_fact_bundle": design_fact_bundle,
                "source_citations": source_citations,
                "competitor_comparison": {},
            },
        )

        analysis_packet = {
            "analysis_summary": analysis_summary,
            "business_scenarios": business_scenarios,
            "ticket_statistics": ticket_summary,
            "ticket_appendix": ticket_appendix,
            "formal_evidence_count": formal_evidence_count,
            "customer_analysis": customer_profiles,
            "capability_analysis": capability_analysis,
            "upstream_downstream_analysis": upstream_downstream_analysis,
            "change_impact": change_impact,
            "functional_architecture": functional_architecture,
            "solution_reference": solution_candidates,
            "problem_essence": problem_essence,
            "requirement_type": requirement_type,
            "module_candidates": module_candidates,
            "solution_generation_status": solution_generation_status,
            "risk_assessment": {
                "risks": risk_summary,
                "effort_estimate_size": effort_estimate_size,
            },
            "design_fact_bundle": design_fact_bundle,
            "pending_decisions": pending_questions,
            "source_citations": source_citations,
        }

        return {
            "module_hint": module_hint,
            "topic_names": evidence_bundle.get("topic_names", req.get("ai_analysis", {}).get("topic_names", [])),
            "evidence_bundle": evidence_bundle,
            "formal_evidence_count": formal_evidence_count,
            "ticket_summary": ticket_summary,
            "ticket_appendix": ticket_appendix,
            "customer_profiles": customer_profiles,
            "related_requirements": related_requirements,
            "business_scenarios": business_scenarios,
            "capability_analysis": capability_analysis,
            "upstream_downstream_analysis": upstream_downstream_analysis,
            "change_impact": change_impact,
            "functional_architecture": functional_architecture,
            "risk_summary": risk_summary,
            "effort_estimate_size": effort_estimate_size,
            "solution_candidates": solution_candidates,
            "problem_essence": problem_essence,
            "requirement_type": requirement_type,
            "module_candidates": module_candidates,
            "solution_generation_status": solution_generation_status,
            "pending_questions": pending_questions,
            "source_citations": source_citations,
            "design_fact_bundle": design_fact_bundle,
            "competitor_dossiers": competitor_dossiers,
            "analysis_summary": analysis_summary,
            "analysis_packet": analysis_packet,
        }

    def _merge_competitor_dossiers(self, competitor_bundle: Dict[str, Any], dossiers: List[Dict[str, Any]]) -> Dict[str, Any]:
        bundle = dict(competitor_bundle or {})
        vendors = list(bundle.get("vendors", []) or [])
        vendor_map = {item.get("vendor", ""): item for item in vendors if item.get("vendor")}
        for dossier in dossiers or []:
            vendor = dossier.get("vendor", "")
            if not vendor:
                continue
            target = vendor_map.get(vendor)
            citation_items = [
                {
                    "title": item.get("title", dossier.get("feature_summary", "")),
                    "url": item.get("url", ""),
                    "source_type": item.get("source_level", "internal_dossier"),
                    "snippet": item.get("snippet", dossier.get("feature_summary", "")),
                }
                for item in (dossier.get("evidence_items") or [])
            ] or [
                {
                    "title": f"{vendor} - {dossier.get('feature_key', '内部档案')}",
                    "url": "",
                    "source_type": "internal_dossier",
                    "snippet": dossier.get("feature_summary", ""),
                }
            ]
            if target is None:
                target = {
                    "vendor": vendor,
                    "implementation_summary": dossier.get("feature_summary", ""),
                    "scenarios": [],
                    "key_capabilities": [],
                    "ui_touchpoints": list(dossier.get("ui_touchpoints", []) or []),
                    "architecture_components": [],
                    "architecture_flow": list(dossier.get("config_levels", []) or []),
                    "key_images": [],
                    "screenshot_targets": [],
                    "borrowable_patterns": list(dossier.get("config_levels", []) or []),
                    "risks_or_limits": list(dossier.get("constraints", []) or []),
                    "citations": [],
                    "evidence_items": [],
                    "verification": {"status": dossier.get("verification_status", "unverified"), "captures": list(dossier.get("captures", []) or []), "notes": [dossier.get("notes", "")] if dossier.get("notes") else []},
                    "confidence": "medium",
                }
                vendors.append(target)
                vendor_map[vendor] = target
            target["implementation_summary"] = target.get("implementation_summary") or dossier.get("feature_summary", "")
            target["ui_touchpoints"] = list(dict.fromkeys((target.get("ui_touchpoints") or []) + (dossier.get("ui_touchpoints") or [])))[:5]
            target["borrowable_patterns"] = list(dict.fromkeys((target.get("borrowable_patterns") or []) + (dossier.get("config_levels") or [])))[:4]
            target["risks_or_limits"] = list(dict.fromkeys((target.get("risks_or_limits") or []) + (dossier.get("constraints") or [])))[:4]
            target["citations"] = list(dict.fromkeys([json.dumps(item, ensure_ascii=False, sort_keys=True) for item in (target.get("citations") or []) + citation_items]))
            target["citations"] = [json.loads(item) for item in target["citations"]][:3]
            target["evidence_items"] = list(dict.fromkeys([json.dumps(item, ensure_ascii=False, sort_keys=True) for item in (target.get("evidence_items") or []) + (dossier.get("evidence_items") or [])]))
            target["evidence_items"] = [json.loads(item) for item in target["evidence_items"]][:3]
            target["verification"] = {
                "status": dossier.get("verification_status", (target.get("verification") or {}).get("status", "unverified")),
                "captures": list(dossier.get("captures", []) or (target.get("verification") or {}).get("captures", [])),
                "notes": list(dict.fromkeys(((target.get("verification") or {}).get("notes", []) or []) + ([dossier.get("notes")] if dossier.get("notes") else []))),
            }
        bundle["vendors"] = vendors
        return bundle

    def _get_competitor_analysis_enhanced(self, requirement: Dict[str, Any]) -> Dict[str, Any]:
        """Enhanced competitor analysis using KB exploration assets + cache + feature matrix.

        Priority: ExplorationAssetRetriever (KB docs/screenshots/prototypes/feature_matrix)
        first, then falls back to research_with_cache() for web-search evidence on gaps.
        """
        import json as _json
        from pathlib import Path

        try:
            from competitor_account_manager import CompetitorAccountManager
            from competitor_research_service import CompetitorResearchService

            mgr = CompetitorAccountManager()
            svc = CompetitorResearchService()

            # 1. Match requirement to feature IDs
            req_text = (
                requirement.get("title", "") + " " + requirement.get("description", "")
            ).strip()
            feature_ids = mgr.match_requirement_to_features(req_text)
            if not feature_ids:
                feature_ids = ["workflow_approval", "delegation"]

            # 2. KB-first: try ExplorationAssetRetriever for pre-explored data
            exploration_assets: Dict[str, Any] = {}
            exploration_gaps: list[Dict[str, Any]] = []
            exploration_guidance: str = ""
            try:
                from exploration_asset_retriever import ExplorationAssetRetriever
                retriever = ExplorationAssetRetriever()
                retrieval = retriever.retrieve_for_requirement(req_text, feature_ids)
                exploration_assets = retrieval.get("results", {})
                exploration_gaps = retrieval.get("gaps", [])
                exploration_guidance = retrieval.get("guidance", "")
            except Exception:
                pass

            # 3. For gaps only, fall back to web search via research_with_cache
            gap_feature_ids = list({g["feature"] for g in exploration_gaps if g.get("reason", "").startswith("未探索")})
            evidence: list[Dict[str, Any]] = []
            if gap_feature_ids:
                evidence = svc.research_with_cache(requirement, gap_feature_ids)
            elif not exploration_assets:
                # No exploration data at all — full fallback
                evidence = svc.research_with_cache(requirement, feature_ids)

            # 4. Load pre-built feature matrices from disk
            matrices: list[Dict[str, Any]] = []
            root = Path.cwd()
            seen_vendors: set[str] = set()
            for fid in feature_ids:
                matrix_path = root / "data_cache" / "competitor_validation" / "feature_matrix" / f"{fid}.json"
                if matrix_path.exists():
                    try:
                        matrices.append(_json.loads(matrix_path.read_text(encoding="utf-8")))
                    except Exception:
                        pass
            # Also load vendor-level matrices (e.g. kingdee.json)
            fm_dir = root / "data_cache" / "competitor_validation" / "feature_matrix"
            if fm_dir.is_dir():
                for vendor_file in fm_dir.glob("*.json"):
                    vendor_id = vendor_file.stem
                    if vendor_id not in seen_vendors:
                        seen_vendors.add(vendor_id)
                        try:
                            data = _json.loads(vendor_file.read_text(encoding="utf-8"))
                            if data not in matrices:
                                matrices.append(data)
                        except Exception:
                            pass

            return {
                "evidence": evidence,
                "feature_matrices": matrices,
                "matched_features": feature_ids,
                "exploration_assets": exploration_assets,
                "exploration_gaps": exploration_gaps,
                "exploration_guidance": exploration_guidance,
            }
        except Exception:
            return {
                "evidence": [],
                "feature_matrices": [],
                "matched_features": [],
                "exploration_assets": {},
                "exploration_gaps": [],
                "exploration_guidance": "",
            }

    def _run_competitor_researcher(
        self,
        req: Dict[str, Any],
        research: Dict[str, Any],
        base_artifact: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        competitor_bundle = {}
        if base_artifact:
            competitor_bundle = ((base_artifact.get("analysis_packet") or {}).get("competitor_comparison") or {})
        if (not self._has_meaningful_competitor_bundle(competitor_bundle)) and self.competitor_research_service:
            try:
                competitor_bundle = self.competitor_research_service.research(req, research.get("analysis_packet", {}), top_k=3) or {}
            except Exception:
                competitor_bundle = {}
        competitor_bundle = self._merge_competitor_dossiers(competitor_bundle, research.get("competitor_dossiers", []))

        competitor_bundle = competitor_bundle or {
            "summary": "暂未检索到充分竞品公开资料，需人工补充验证。",
            "vendors": [],
            "pending_questions": ["需人工补充 SAP、金蝶、泛微、致远的公开方案证据。"],
        }

        analysis_packet = dict(research.get("analysis_packet") or {})
        analysis_packet["competitor_comparison"] = competitor_bundle
        pending_questions = list(dict.fromkeys((research.get("pending_questions") or []) + (competitor_bundle.get("pending_questions") or [])))
        analysis_summary = self._build_analysis_summary(
            req,
            {
                **research,
                "analysis_packet": analysis_packet,
                "competitor_comparison": competitor_bundle,
                "pending_questions": pending_questions,
            },
        )
        return {
            **research,
            "competitor_comparison": competitor_bundle,
            "pending_questions": pending_questions,
            "analysis_summary": analysis_summary,
            "analysis_packet": analysis_packet,
        }

    def _run_quality_guardian_bundle(self, req: Dict[str, Any], research: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        competitor_bundle = research.get("competitor_comparison", {}) or {}
        if not self.competitor_research_service and not self._has_meaningful_competitor_bundle(competitor_bundle):
            assessment = {
                "target_type": "competitor_bundle",
                "decision": "passed",
                "scores": {"richness": 3, "reliability": 3, "value": 3, "relevance": 3},
                "blocking_issues": [],
                "improvement_actions": ["当前未启用竞品研究，后续如需对外比对可单独补充。"],
                "summary": "竞品研究未启用，跳过严格证据门禁。",
            }
            analysis_packet = dict(research.get("analysis_packet") or {})
            analysis_packet["competitor_comparison"] = competitor_bundle
            return {
                **research,
                "competitor_comparison": competitor_bundle,
                "analysis_packet": analysis_packet,
                "quality_assessment": assessment,
            }, assessment
        filtered_bundle, assessment = self.competitor_quality_guardian.evaluate_bundle(competitor_bundle, req)
        analysis_packet = dict(research.get("analysis_packet") or {})
        analysis_packet["competitor_comparison"] = filtered_bundle
        analysis_summary = self._build_analysis_summary(
            req,
            {
                **research,
                "analysis_packet": analysis_packet,
                "competitor_comparison": filtered_bundle,
            },
        )
        updated = {
            **research,
            "competitor_comparison": filtered_bundle,
            "analysis_packet": analysis_packet,
            "analysis_summary": analysis_summary,
            "quality_assessment": assessment,
        }
        if assessment.get("decision") in {"warning", "failed"}:
            warning_message = assessment.get("summary") or "竞品证据不足，已降级为提示信息。"
            filtered_bundle["evidence_status"] = "insufficient" if assessment.get("decision") == "warning" else "invalid"
            filtered_bundle["warning_message"] = warning_message
            return {
                **updated,
                "competitor_comparison": filtered_bundle,
                "analysis_packet": {**analysis_packet, "competitor_comparison": filtered_bundle},
                "pending_questions": list(
                    dict.fromkeys((updated.get("pending_questions") or []) + assessment.get("blocking_issues", []) + assessment.get("improvement_actions", []))
                ),
            }, assessment
        return updated, assessment

    def _run_quality_guardian_artifact(
        self,
        artifact: Dict[str, Any],
        bundle_assessment: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        competitor_bundle = artifact.get("competitor_comparison", {}) or (artifact.get("analysis_packet", {}) or {}).get("competitor_comparison", {})
        if not self.competitor_research_service and not self._has_meaningful_competitor_bundle(competitor_bundle):
            artifact_assessment = {
                "target_type": "draft_content",
                "decision": "passed",
                "solution_decision": "passed",
                "competitor_decision": "passed",
                "scores": {"richness": 3, "reliability": 3, "value": 3, "relevance": 3},
                "blocking_issues": [],
                "improvement_actions": ["当前草稿未启用竞品严格校验，重点继续走结构与场景质量检查。"],
                "summary": "竞品质量把关已跳过，继续执行结构质量检查。",
            }
            artifact["quality_assessment"] = artifact_assessment
            return artifact, artifact_assessment

        competitor_assessment = self.competitor_quality_guardian.evaluate_artifact(artifact)
        if bundle_assessment:
            competitor_assessment["scores"] = {
                key: min(
                    int((bundle_assessment.get("scores", {}) or {}).get(key, competitor_assessment["scores"].get(key, 3)) or 3),
                    int(competitor_assessment["scores"].get(key, 3) or 3),
                )
                for key in ("richness", "reliability", "value", "relevance")
            }
            if bundle_assessment.get("decision") == "warning":
                competitor_assessment["decision"] = "warning"
                competitor_assessment["blocking_issues"] = list(
                    dict.fromkeys((competitor_assessment.get("blocking_issues", []) or []) + (bundle_assessment.get("blocking_issues", []) or []))
                )
                competitor_assessment["summary"] = bundle_assessment.get("summary") or "竞品证据不足，已降级为告警。"
            elif bundle_assessment.get("decision") != "passed":
                competitor_assessment["decision"] = "failed"
                competitor_assessment["blocking_issues"] = list(
                    dict.fromkeys((competitor_assessment.get("blocking_issues", []) or []) + (bundle_assessment.get("blocking_issues", []) or []))
                )
        solution_assessment = self._build_solution_quality_assessment(artifact)
        overall_decision = "passed"
        if solution_assessment["decision"] == "failed":
            overall_decision = "failed"
        elif solution_assessment["decision"] == "warning" or competitor_assessment.get("decision") in {"warning", "failed"}:
            overall_decision = "warning"
        artifact_assessment = {
            **competitor_assessment,
            "decision": overall_decision,
            "solution_decision": solution_assessment["decision"],
            "competitor_decision": competitor_assessment.get("decision", "passed"),
            "solution_blocking_issues": solution_assessment.get("blocking_issues", []),
            "competitor_blocking_issues": competitor_assessment.get("blocking_issues", []),
            "blocking_issues": solution_assessment.get("blocking_issues", []) if solution_assessment["decision"] == "failed" else list(
                dict.fromkeys((solution_assessment.get("blocking_issues", []) or []) + (competitor_assessment.get("blocking_issues", []) or []))
            ),
            "summary": solution_assessment.get("summary") if solution_assessment["decision"] == "failed" else (
                solution_assessment.get("summary") if solution_assessment["decision"] == "warning" else competitor_assessment.get("summary", "内容质量通过")
            ),
        }
        artifact["quality_assessment"] = artifact_assessment
        if solution_assessment["decision"] == "failed":
            raise DraftQualityError(
                solution_assessment.get("summary") or "初稿未通过质量门禁，请先补足内部设计事实。",
                artifact=artifact,
                issues=solution_assessment.get("blocking_issues", []),
                stage="quality_guardian",
            )
        return artifact, artifact_assessment

    def _build_solution_quality_assessment(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        design_fact_bundle = artifact.get("design_fact_bundle", {}) or (artifact.get("analysis_packet", {}) or {}).get("design_fact_bundle", {})
        coverage = design_fact_bundle.get("coverage_summary", {}) or {}
        missing_facts = design_fact_bundle.get("missing_facts", []) or []
        formal_evidence_count = int(artifact.get("formal_evidence_count", 0) or 0)
        ticket_count = int((artifact.get("ticket_summary", {}) or {}).get("matched_ticket_count", 0) or 0)
        total_fact_count = int(coverage.get("total_fact_count", 0) or 0)

        blocking_issues = []
        decision = "passed"
        if total_fact_count == 0 and formal_evidence_count == 0 and ticket_count == 0:
            decision = "failed"
            blocking_issues.append("缺少可支撑方案设计的内部产品事实、KB资料和工单样本。")
        elif int(coverage.get("missing_fact_count", 0) or 0) >= 4:
            decision = "warning"
            blocking_issues.append("流程级、环节级、租户级或单据属性级仍有多项关键事实缺失。")
        elif missing_facts and total_fact_count < 2:
            decision = "warning"
            blocking_issues.append("内部设计事实仍偏少，当前方案存在边界判断偏差风险。")

        return {
            "target_type": "solution_design",
            "decision": decision,
            "blocking_issues": list(dict.fromkeys(blocking_issues + missing_facts[:4] if decision != "passed" else blocking_issues)),
            "summary": (
                "内部设计事实不足，当前方案无法稳定成立。"
                if decision == "failed"
                else "内部设计事实仍有缺口，建议补充后继续细化方案。"
                if decision == "warning"
                else "内部设计事实已能支撑当前方案设计。"
            ),
        }

    def _has_meaningful_competitor_bundle(self, bundle: Dict[str, Any]) -> bool:
        vendors = (bundle or {}).get("vendors", []) or []
        if not vendors:
            return False
        for vendor in vendors:
            if (vendor.get("citations") or []) or (vendor.get("implementation_summary") or "").strip():
                return True
        return False

    def _compute_section_diff(self, base: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        """对比 base/new artifact 的三个 LLM 章节，输出逐 item 的 diff_from_prev。"""
        _sections = {
            "solution_candidates": ("title", lambda a: a.get("solution_candidates") or []),
            "change_impact": ("surface", lambda a: a.get("change_impact") or []),
            "upstream_downstream_analysis": ("surface", lambda a: a.get("upstream_downstream_analysis") or []),
        }
        result: Dict[str, Any] = {}
        for section_name, (id_field, extractor) in _sections.items():
            base_map = {
                item.get(id_field, ""): item
                for item in extractor(base)
                if isinstance(item, dict)
            }
            diffs = []
            for item in extractor(new):
                if not isinstance(item, dict):
                    continue
                key = item.get(id_field, "")
                base_item = base_map.get(key)
                if base_item is None:
                    diffs.append({
                        "item_id": key,
                        "diff_from_prev": {
                            "added_fields": {k: v for k, v in item.items() if not k.startswith("changed_")},
                            "modified_fields": {},
                            "removed_fields": {},
                        },
                    })
                else:
                    _skip = {"changed_in_iteration", "linked_revision_comment", "llm_reasoning", "source", "generation_status"}
                    added, modified, removed = {}, {}, {}
                    for k, v in item.items():
                        if k in _skip:
                            continue
                        bv = base_item.get(k)
                        if bv is None:
                            added[k] = v
                        elif bv != v:
                            modified[k] = {"old": bv, "new": v}
                    for k, v in base_item.items():
                        if k not in item and k not in _skip:
                            removed[k] = v
                    if added or modified or removed:
                        diffs.append({
                            "item_id": key,
                            "diff_from_prev": {"added_fields": added, "modified_fields": modified, "removed_fields": removed},
                        })
            result[section_name] = diffs
        return result

    def _artifact_to_research(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        evidence_bundle = artifact.get("evidence_bundle", {}) or {}
        return {
            "module_hint": artifact.get("module_hint", ""),
            "topic_names": artifact.get("topic_names", []),
            "evidence_bundle": evidence_bundle,
            "formal_evidence_count": artifact.get("formal_evidence_count", self._formal_evidence_count(evidence_bundle)),
            "ticket_summary": artifact.get("ticket_summary", {}),
            "ticket_appendix": artifact.get("ticket_appendix", []),
            "customer_profiles": artifact.get("customer_profiles", []),
            "related_requirements": artifact.get("related_requirements", []),
            "business_scenarios": artifact.get("business_scenarios", []),
            "capability_analysis": artifact.get("capability_analysis", {}),
            "upstream_downstream_analysis": artifact.get("upstream_downstream_analysis", []),
            "change_impact": artifact.get("change_impact", []),
            "functional_architecture": artifact.get("functional_architecture", []),
            "risk_summary": artifact.get("risk_summary", []),
            "effort_estimate_size": artifact.get("effort_estimate_size", ""),
            "solution_candidates": artifact.get("solution_candidates", []),
            "problem_essence": artifact.get("problem_essence", ""),
            "requirement_type": artifact.get("requirement_type", ""),
            "module_candidates": artifact.get("module_candidates", []),
            "solution_generation_status": artifact.get("solution_generation_status", ""),
            "pending_questions": artifact.get("pending_questions", []),
            "source_citations": artifact.get("source_citations", []),
            "analysis_packet": artifact.get("analysis_packet", {}),
            "competitor_comparison": artifact.get("competitor_comparison", {}) or (artifact.get("analysis_packet", {}) or {}).get("competitor_comparison", {}),
            "design_fact_bundle": artifact.get("design_fact_bundle", {}) or (artifact.get("analysis_packet", {}) or {}).get("design_fact_bundle", {}),
            "competitor_dossiers": artifact.get("competitor_dossiers", []),
            "analysis_summary": artifact.get("analysis_summary", {}) or (artifact.get("analysis_packet", {}) or {}).get("analysis_summary", {}),
            "quality_assessment": artifact.get("quality_assessment", {}),
            "revision_history": artifact.get("revision_history", []),
            "revision_iteration": artifact.get("revision_iteration", 0),
            "section_diffs": artifact.get("section_diffs", {}),
        }

    def _format_ticket_case_lines(self, appendix: List[Dict[str, Any]], limit: int = 5) -> str:
        lines = []
        for item in appendix[:limit]:
            lines.append(
                f"- {item.get('issue_key', '')} | {item.get('summary', '')} | 模块: {item.get('module', '')} | 客户特征: {item.get('customer_type', '')}"
            )
        return "\n".join(lines) if lines else "- 暂无典型工单案例"

    def _format_customer_profiles(self, profiles: List[Dict[str, Any]]) -> str:
        if not profiles:
            return "- 暂无明显客户画像"
        return "\n".join(
            f"- {profile['label']}：{profile['analysis']} 代表工单: {', '.join(profile.get('representative_tickets', [])) or '无'}"
            for profile in profiles
        )

    def _format_business_scenarios(self, scenarios: List[Dict[str, Any]]) -> str:
        if not scenarios:
            return "- 当前未形成可用业务场景分析。"
        blocks = []
        for scenario in scenarios:
            blocks.append(
                "\n".join(
                    [
                        f"### {scenario.get('title', '未命名场景')}",
                        f"- 参与角色：{'、'.join(scenario.get('actors', [])) or '待确认'}",
                        f"- 触发条件：{scenario.get('trigger', '待确认')}",
                        f"- 场景分析：{scenario.get('summary', '待确认')}",
                        f"- 业务价值：{scenario.get('expected_value', '待确认')}",
                        f"- 证据引用：{'；'.join(scenario.get('citations', [])) or '无'}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _format_capability_analysis(self, capability_analysis: Dict[str, Any]) -> str:
        capabilities = capability_analysis.get("current_capabilities", []) or []
        gaps = capability_analysis.get("coverage_gaps", []) or []
        lines = []
        if capabilities:
            lines.append("### 现有能力")
            lines.extend(
                f"- {item.get('title', '未命名能力')}：{item.get('summary', '')}（来源：{item.get('citation', item.get('source_kind', ''))}）"
                for item in capabilities
            )
        if gaps:
            lines.append("### 能力缺口")
            lines.extend(f"- {gap}" for gap in gaps)
        return "\n".join(lines) if lines else "- 当前未形成可用能力分析。"

    def _format_upstream_downstream_analysis(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return "- 当前未命中上下游能力证据，需人工补充功能边界。"
        return "\n".join(
            f"- {item.get('surface', '未命名能力')}（{item.get('connection', 'unknown')}）：{item.get('summary', '')}"
            for item in items
        )

    def _format_change_impact(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return "- 当前未形成可用改动影响面分析。"
        return "\n".join(
            f"- {item.get('surface', '未命名模块')}：{item.get('summary', '')}"
            for item in items
        )

    def _format_related_requirements(self, related_requirements: List[Dict[str, Any]]) -> str:
        if not related_requirements:
            return "- 暂无高相关需求"
        return "\n".join(
            f"- {item['req_id']} {item['title']}（相似度 {item['score']:.2f}，状态 {item['status'] or '未知'}）"
            for item in related_requirements
        )

    def _format_upstream_connections(self, evidence_bundle: Dict[str, Any]) -> str:
        evidence = evidence_bundle.get("evidence", []) or []
        if not evidence:
            return "- 当前未命中上下游能力证据，需人工补充功能边界。"
        lines = []
        for item in evidence[:5]:
            lines.append(f"- {item.get('name', '')}（{item.get('source_kind', '')}）：{item.get('summary', '')}")
        return "\n".join(lines)

    def _format_solution_candidates(
        self,
        candidates: List[Dict[str, Any]],
        problem_essence: str = "",
        requirement_type: str = "",
        module_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if not candidates:
            return "- 暂无候选方案"
        lines: List[str] = []

        # 顶部：问题本质 + 候选模块 概览
        if problem_essence:
            rt = requirement_type or "其他"
            lines.append(f"**问题本质**: {problem_essence}（类型：{rt}）")
        if module_candidates:
            module_tokens: List[str] = []
            for mc in module_candidates[:6]:
                name = (mc.get("name") if isinstance(mc, dict) else "") or ""
                if name:
                    module_tokens.append(name.strip())
            if module_tokens:
                lines.append(f"**候选模块**: {' / '.join(module_tokens)}")
        if lines:
            lines.append("")

        for candidate in candidates:
            title = candidate.get("title", "未命名方案")
            target_module = candidate.get("target_module", "")
            change_type = candidate.get("change_type", "")
            product_layer = candidate.get("product_layer", "待确认")
            description = candidate.get("description", "")
            source = candidate.get("source", "unknown")
            applicable_when = candidate.get("applicable_when", "")
            pros = candidate.get("pros", []) or []
            cons = candidate.get("cons", []) or []

            # 优先展示 target_module/change_type；老数据无该字段时退回 product_layer
            header_bits = []
            if target_module:
                header_bits.append(f"目标模块：{target_module}")
            if change_type:
                header_bits.append(f"改造动作：{change_type}")
            if not header_bits:
                header_bits.append(f"层级：{product_layer}")
            header_bits.append(f"来源：{source}")
            lines.append(f"- {title}（{'，'.join(header_bits)}）")
            if description:
                lines.append(f"  - 方案说明：{description}")
            if applicable_when:
                lines.append(f"  - 适用场景：{applicable_when}")
            if pros:
                lines.append(f"  - 优点：{'；'.join(str(item) for item in pros[:3] if item)}")
            if cons:
                lines.append(f"  - 约束：{'；'.join(str(item) for item in cons[:3] if item)}")
        return "\n".join(lines)

    def _format_solution_comparison(self, candidates: List[Dict[str, Any]], selected_solution: Optional[Dict[str, Any]] = None) -> str:
        """生成方案比较与选择 Markdown 章节。"""
        if not candidates:
            return "暂无方案比较数据。"
        selected_idx = selected_solution.get("index", -1) if selected_solution else -1
        lines = ["### 候选方案对比", "",
                 "| 方案 | 描述 | 优势 | 劣势 | 适用条件 |",
                 "|------|------|------|------|----------|"]
        for i, c in enumerate(candidates):
            mark = "✅ " if i == selected_idx else ""
            title = c.get("title", "未命名")
            desc = c.get("description", "")[:80]
            pros = "；".join(str(p) for p in (c.get("pros") or [])[:2])
            cons = "；".join(str(p) for p in (c.get("cons") or [])[:2])
            when = c.get("applicable_when", "")[:60]
            lines.append(f"| {mark}{title} | {desc} | {pros} | {cons} | {when} |")
        lines.append("")
        if selected_solution:
            lines.append("### 选择结果")
            lines.append(f"**采用方案**: {selected_solution.get('title', '未选择')}")
            notes = selected_solution.get("notes", "")
            if notes:
                lines.append(f"**选择原因**: {notes}")
        else:
            lines.append("### 选择结果")
            lines.append("**尚未选择方案** — 需在需求池中选择后再生成 PRD。")
        return "\n".join(lines)

    def _format_functional_architecture(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return "- 当前未形成可用功能架构参考。"
        return "\n".join(
            f"- {item.get('component', '未命名组件')}：{item.get('responsibility', '')}"
            for item in items
        )

    def _format_competitor_comparison(self, competitor_comparison: Dict[str, Any]) -> str:
        vendors = competitor_comparison.get("vendors", []) or []
        if not vendors:
            warning_message = competitor_comparison.get("warning_message") or "暂未检索到充分竞品公开资料，需人工补充验证。"
            return f"- {warning_message}"
        blocks = []
        selected_vendors = (competitor_comparison.get("scope", {}) or {}).get("selected_vendors", []) or []
        if selected_vendors:
            blocks.append("**本轮聚焦竞品**\n- " + "、".join(selected_vendors[:4]))

        # 加载探索资产（截图+原型）
        exploration_assets = self._load_exploration_assets(competitor_comparison)

        for vendor in vendors:
            citations = "；".join(item.get("title", "") for item in vendor.get("citations", []) if item.get("title")) or "无"
            patterns = "、".join(vendor.get("borrowable_patterns", []) or []) or "待确认"
            touchpoints = "、".join(vendor.get("ui_touchpoints", []) or []) or "待确认"
            limits = "；".join(vendor.get("risks_or_limits", []) or []) or "待确认"
            verification = (vendor.get("verification", {}) or {}).get("status", "unverified")
            vendor_name = vendor.get("vendor", "未知厂商")
            lines = [
                f"### {vendor_name}",
                f"- 能力判断：{'支持该需求点' if vendor.get('feature_match') else '证据不足'}",
                f"- 证据摘要：{vendor.get('implementation_summary', '待确认')}",
                f"- 页面落点：{touchpoints}",
                f"- 可借鉴模式：{patterns}",
                f"- 真实验证：{verification}",
                f"- 限制/风险：{limits}",
                f"- 公开来源：{citations}",
            ]
            # 嵌入探索截图和原型链接
            assets = exploration_assets.get(vendor_name.lower(), {})
            screenshots = assets.get("screenshots", [])
            prototypes = assets.get("prototypes", [])
            if screenshots:
                lines.append(f"- 界面截图（{len(screenshots)}张）：")
                for ss in screenshots[:5]:
                    lines.append(f"  - [{ss['name']}]({ss['url']})")
            if prototypes:
                lines.append(f"- 原型演示（{len(prototypes)}个）：")
                for pt in prototypes:
                    lines.append(f"  - [{pt['name']}]({pt['url']})")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _load_exploration_assets(self, competitor_comparison: Dict[str, Any]) -> Dict[str, Dict]:
        """从 conclusion/ 目录加载各竞品的截图和原型资产。"""
        conclusion_dir = os.path.join(PROJECT_ROOT, "conclusion")
        vendor_map = {
            "bip": "bip-workflow", "用友": "bip-workflow",
            "金蝶": "kingdee-workflow", "kingdee": "kingdee-workflow",
            "泛微": "weaver-workflow", "致远": "seeyon-workflow",
            "sap": "sap-workflow",
        }
        assets: Dict[str, Dict] = {}
        for vendor_key, dir_name in vendor_map.items():
            vendor_dir = os.path.join(conclusion_dir, dir_name)
            if not os.path.isdir(vendor_dir):
                continue
            ss_dir = os.path.join(vendor_dir, "screenshots")
            pt_dir = os.path.join(vendor_dir, "prototype")
            screenshots = []
            if os.path.isdir(ss_dir):
                for f in sorted(os.listdir(ss_dir))[:10]:
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
                        screenshots.append({"name": f, "url": f"/conclusion/{dir_name}/screenshots/{f}"})
            prototypes = []
            if os.path.isdir(pt_dir):
                for d in sorted(os.listdir(pt_dir)):
                    idx = os.path.join(pt_dir, d, "index.html")
                    if os.path.isfile(idx):
                        prototypes.append({"name": d, "url": f"/conclusion/{dir_name}/prototype/{d}/index.html"})
            if screenshots or prototypes:
                assets[vendor_key] = {"screenshots": screenshots, "prototypes": prototypes}
        return assets

    def _build_prd_mapping_markdown(self, research: Dict[str, Any]) -> str:
        change_impact = research.get("change_impact", [])
        risks = research.get("risk_summary", [])
        summary = research.get("module_hint") or "当前模块"
        impacts = [item.get("surface", "待确认模块") for item in change_impact[:6]] or ["待确认模块"]
        architecture = research.get("functional_architecture", []) or []
        capability_lines = [
            f"- {item.get('responsibility', '')}"
            for item in architecture[:4]
            if item.get("responsibility")
        ] or ["- 需结合现有交互、规则和留痕能力补齐当前需求。"]
        interaction_lines = [
            f"- {item.get('summary', '')}"
            for item in change_impact[:4]
            if item.get("summary")
        ] or ["- 需明确当前页面的触发入口、提示文案和展示位置。"]
        lines = [
            "## 目标与范围",
            f"- 建议把当前需求定义为 {summary} 内的增量能力，而不是拆成新的孤立模块。",
            "",
            "## 模块影响面",
        ]
        lines.extend(f"- {surface}" for surface in impacts)
        lines.extend(["", "## 关键能力"])
        lines.extend(capability_lines)
        lines.extend(["", "## 交互与展示"])
        lines.extend(interaction_lines)
        lines.extend(["", "## 边界与验收建议"])
        lines.extend(f"- {risk}" for risk in risks[:4] or ["待结合风险分析补充。"])
        return "\n".join(lines)

    def _build_business_analysis_markdown(
        self,
        req: Dict[str, Any],
        plan: Dict[str, Any],
        research: Dict[str, Any],
        mapped_prd_content: str,
        draft_type: str,
        revision_comments: str = "",
        previous_content: str = "",
    ) -> str:
        ticket_summary = research["ticket_summary"]
        clue_bundle = self._extract_requirement_clues(req)
        focus = self._extract_requirement_focus(req, research.get("module_hint", ""), research.get("evidence_bundle", {}))
        analysis_summary = research.get("analysis_summary") or self._build_analysis_summary(req, research)
        top_modules = "、".join(item["name"] for item in ticket_summary.get("top_modules", [])) or "待识别模块"
        customer_types = "、".join(item["name"] for item in ticket_summary.get("customer_type_distribution", [])) or "来源工单样本"
        topic_values = research.get("topic_names", []) or clue_bundle.get("labels", []) or clue_bundle.get("modules", [])
        topic_names = "、".join(topic_values) or focus.get("summary") or "当前需求场景"
        module_display = (
            research.get("module_hint")
            or (clue_bundle.get("modules", []) or [""])[0]
            or req.get("ai_analysis", {}).get("module")
            or "流程中心"
        )
        background_root_cause = self._derive_background_root_cause(req, research)
        heading = "业务分析型概要需求初稿" if draft_type == "summary" else "业务分析型详细需求初稿"
        internal_reference_lines = [
            f"- 内部资料 | {item.get('title', '未命名资料')} | {item.get('source', 'internal')} | {item.get('reason', '')}"
            for item in (analysis_summary.get("internal_references") or [])
        ] or ["- 暂无内部资料索引"]
        external_reference_lines = [
            f"- 外部资料 | {item.get('title', '未命名资料')} | {item.get('source', 'external')} | {item.get('reason', '')}"
            for item in (analysis_summary.get("external_references") or [])
        ] or ["- 暂无外部资料索引"]

        parts = [
            f"# {heading} - {req.get('title', '')}",
            "## 需求背景与问题定义",
            f"- 需求编号：{req.get('req_id', '')}",
            f"- 当前模块判断：{module_display}",
            f"- 核心问题：{analysis_summary.get('core_problem') or background_root_cause}",
            f"- 当前产品现状：{analysis_summary.get('current_product_behavior') or '待确认'}",
            f"- 主要缺口：{analysis_summary.get('gap_analysis') or background_root_cause}",
            f"- 根因分析：{background_root_cause}",
            f"- 产品层级：{analysis_summary.get('product_layer') or '待确认'}",
            f"- 场景关键词：{'、'.join(analysis_summary.get('scenario_keywords', []) or []) or '待补充'}",
            f"- Topic：{topic_names}",
            "",
            "## 业务场景拆解",
            self._format_business_scenarios(research["business_scenarios"]),
            "",
            "## 全量工单统计与典型案例",
            f"- 命中相关工单数量：{ticket_summary.get('matched_ticket_count', 0)}",
            f"- 原始命中工单数量：{ticket_summary.get('matched_ticket_count_raw', ticket_summary.get('matched_ticket_count', 0))}",
            f"- 已过滤低相关工单：{ticket_summary.get('filtered_low_relevance_count', 0)}",
            f"- 正式资料数量：{research.get('formal_evidence_count', 0)}",
            f"- 高频模块分布：{top_modules}",
            f"- 客户类型分布：{customer_types}",
            f"- 典型工单：{', '.join(ticket_summary.get('typical_case_keys', [])) or '无'}",
            f"- 工单证据定位：{(analysis_summary.get('ticket_evidence_summary') or {}).get('positioning') or '待补充'}",
            f"- 价值证据：{(analysis_summary.get('ticket_evidence_summary') or {}).get('value_statement') or '待补充'}",
            f"- 场景证据：{(analysis_summary.get('ticket_evidence_summary') or {}).get('scenario_statement') or '待补充'}",
            "",
            self._format_ticket_case_lines(research["ticket_appendix"]),
            "",
            "## 客户业务特征分析",
            self._format_customer_profiles(research["customer_profiles"]),
            "",
            "## 现有能力与缺口分析",
            self._format_capability_analysis(research["capability_analysis"]),
            "",
            "## 上下游功能结合点与场景连通性",
            self._format_upstream_downstream_analysis(research["upstream_downstream_analysis"]),
            "",
            "## 功能改动点与影响面",
            self._format_change_impact(research["change_impact"]),
            "",
            "## 风险点与改动量粗估",
            *[f"- {risk}" for risk in research["risk_summary"]],
            f"- 改动量粗估：{research['effort_estimate_size']}",
            "",
            "## 产品价值总结",
            f"- 问题覆盖：{(analysis_summary.get('product_value') or {}).get('problem_coverage') or '待补充'}",
            f"- 客户价值：{(analysis_summary.get('product_value') or {}).get('customer_value') or '待补充'}",
            f"- 长期价值：{(analysis_summary.get('product_value') or {}).get('long_term_value') or '待补充'}",
            "",
            "## 方案与功能架构参考",
            self._format_solution_candidates(
                research["solution_candidates"],
                research.get("problem_essence", ""),
                research.get("requirement_type", ""),
                research.get("module_candidates", []),
            ),
            "",
            self._format_functional_architecture(research["functional_architecture"]),
            "",
            "## 方案比较与选择",
            self._format_solution_comparison(
                research["solution_candidates"],
                research.get("selected_solution") or req.get("ai_analysis", {}).get("selected_solution"),
            ),
            "",
            "## 参考资料索引",
            *internal_reference_lines,
            *external_reference_lines,
            "",
            "## 竞品实现分析",
            self._format_competitor_comparison(research.get("competitor_comparison", {})),
            "",
            "## 待人工确认项",
            "\n".join(f"- {question}" for question in research["pending_questions"]) or "- 暂无",
            "",
        ]
        if revision_comments:
            parts.extend(
                [
                    "## 本轮审核意见与修订响应",
                    f"- 人工审核意见：{revision_comments}",
                    "- 已根据本轮审核意见补充场景、风险和加工建议，请在提交前再次核对关键边界。",
                    "",
                ]
            )
        if draft_type == "detail":
            parts.extend(
                [
                    "## 详细方案展开",
                    "- 建议按业务对象、查询维度、页面交互、权限规则、异常场景分别细化详细需求。",
                    "- 本节重点用于承接后续需求规划模块中的终版 PRD 细化。",
                    "",
                ]
            )
        if previous_content:
            parts.extend(
                [
                    "## 上一版初稿保留要点",
                    previous_content[:1500],
                    "",
                ]
            )
        parts.extend(
            [
                "## PRD映射建议",
                mapped_prd_content.strip() or "待补充。",
                "",
                "## 工单全量附录",
                "\n".join(
                    f"- {item['issue_key']} | {item['summary']} | 模块:{item['module']} | 客户特征:{item['customer_type']} | 分数:{item['score']:.2f}"
                    for item in research["ticket_appendix"]
                ) or "- 暂无",
                "",
            ]
        )
        return "\n".join(parts).strip() + "\n"

    def _maybe_generate_prd_mapping(self, req_id: str, draft_type: str) -> str:
        return ""

    def _run_writer(self, draft_id: str, req: Dict[str, Any], plan: Dict[str, Any], research: Dict[str, Any], draft_type: str) -> Dict[str, Any]:
        mapped_prd_content = self._build_prd_mapping_markdown(research)
        markdown = self._build_business_analysis_markdown(req, plan, research, mapped_prd_content, draft_type)
        safe_title = getattr(self.spec_generator, "_clean_filename", lambda value: str(value).replace("/", "_"))(req.get("title", "Unknown"))
        heading = "业务分析型概要需求初稿" if draft_type == "summary" else "业务分析型详细需求初稿"
        filename = f"MC-{req['req_id']}-{safe_title}-{heading}.md"
        spec_path = os.path.join(DRAFT_OUTPUT_DIR, filename)
        with open(spec_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)

        return {
            "draft_id": draft_id,
            "req_id": req["req_id"],
            "draft_type": draft_type,
            "version": 1,
            "base_draft_id": "",
            "revision_comments": "",
            "submitted_to_planning": False,
            "ready_to_submit": False,
            "draft_status": "draft_review",
            "spec_file": filename,
            "spec_path": spec_path,
            "draft_content": markdown,
            "draft_excerpt": markdown[:1200],
            "source": "req_pool_mission_control",
            "title": req.get("title", ""),
            "module_hint": research["module_hint"] or req.get("ai_analysis", {}).get("module", ""),
            "topic_names": research["topic_names"],
            "evidence_bundle": research["evidence_bundle"],
            "formal_evidence_count": research["formal_evidence_count"],
            "ticket_summary": research["ticket_summary"],
            "ticket_appendix": research["ticket_appendix"],
            "customer_profiles": research["customer_profiles"],
            "business_scenarios": research["business_scenarios"],
            "capability_analysis": research["capability_analysis"],
            "upstream_downstream_analysis": research["upstream_downstream_analysis"],
            "change_impact": research["change_impact"],
            "functional_architecture": research["functional_architecture"],
            "risk_summary": research["risk_summary"],
            "effort_estimate_size": research["effort_estimate_size"],
            "solution_candidates": research["solution_candidates"],
            "problem_essence": research.get("problem_essence", ""),
            "requirement_type": research.get("requirement_type", ""),
            "module_candidates": research.get("module_candidates", []),
            "solution_generation_status": research.get("solution_generation_status", ""),
            "competitor_comparison": research.get("competitor_comparison", {}),
            "design_fact_bundle": research.get("design_fact_bundle", {}),
            "competitor_dossiers": research.get("competitor_dossiers", []),
            "pending_questions": research["pending_questions"],
            "related_requirements": research["related_requirements"],
            "source_citations": research["source_citations"],
            "analysis_summary": research.get("analysis_summary", self._build_analysis_summary(req, research)),
            "analysis_packet": research["analysis_packet"],
            "quality_assessment": research.get("quality_assessment", {}),
            "quality_gate": {"status": "pending", "missing_modules": [], "quality_issues": []},
            "created_at": datetime.now().isoformat(),
        }

    def _run_rewriter(
        self,
        draft_id: str,
        req: Dict[str, Any],
        plan: Dict[str, Any],
        research: Dict[str, Any],
        base_artifact: Dict[str, Any],
        revision_comments: str,
    ) -> Dict[str, Any]:
        previous_content = self._read_text_file(base_artifact.get("spec_path", ""))
        mapped_prd_content = self._build_prd_mapping_markdown(research)
        markdown = self._build_business_analysis_markdown(
            req,
            plan,
            research,
            mapped_prd_content,
            base_artifact.get("draft_type", "summary"),
            revision_comments=revision_comments,
            previous_content=previous_content,
        )
        safe_title = getattr(self.spec_generator, "_clean_filename", lambda value: str(value).replace("/", "_"))(req.get("title", "Unknown"))
        filename = f"MC-{req['req_id']}-{safe_title}-v{int(base_artifact.get('version', 1)) + 1}-修订.md"
        spec_path = os.path.join(DRAFT_OUTPUT_DIR, filename)
        with open(spec_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)

        artifact = {
            **base_artifact,
            "draft_id": draft_id,
            "version": int(base_artifact.get("version", 1)) + 1,
            "base_draft_id": base_artifact.get("draft_id"),
            "revision_comments": revision_comments,
            "submitted_to_planning": False,
            "ready_to_submit": False,
            "draft_status": "draft_review",
            "spec_file": filename,
            "spec_path": spec_path,
            "draft_content": markdown,
            "draft_excerpt": markdown[:1200],
            "created_at": datetime.now().isoformat(),
        }
        artifact.update(self._artifact_to_research(base_artifact))
        artifact["title"] = req.get("title", "")
        artifact["module_hint"] = research["module_hint"] or artifact.get("module_hint", "")
        artifact["topic_names"] = research["topic_names"]
        artifact["evidence_bundle"] = research["evidence_bundle"]
        artifact["formal_evidence_count"] = research["formal_evidence_count"]
        artifact["ticket_summary"] = research["ticket_summary"]
        artifact["ticket_appendix"] = research["ticket_appendix"]
        artifact["customer_profiles"] = research["customer_profiles"]
        artifact["business_scenarios"] = research["business_scenarios"]
        artifact["capability_analysis"] = research["capability_analysis"]
        artifact["upstream_downstream_analysis"] = research["upstream_downstream_analysis"]
        artifact["change_impact"] = research["change_impact"]
        artifact["functional_architecture"] = research["functional_architecture"]
        artifact["risk_summary"] = research["risk_summary"]
        artifact["effort_estimate_size"] = research["effort_estimate_size"]
        artifact["solution_candidates"] = research["solution_candidates"]
        artifact["problem_essence"] = research.get("problem_essence", "")
        artifact["requirement_type"] = research.get("requirement_type", "")
        artifact["module_candidates"] = research.get("module_candidates", [])
        artifact["solution_generation_status"] = research.get("solution_generation_status", "")
        artifact["competitor_comparison"] = research.get("competitor_comparison", {})
        artifact["design_fact_bundle"] = research.get("design_fact_bundle", {})
        artifact["competitor_dossiers"] = research.get("competitor_dossiers", [])
        artifact["pending_questions"] = research["pending_questions"]
        artifact["related_requirements"] = research["related_requirements"]
        artifact["source_citations"] = research["source_citations"]
        artifact["analysis_summary"] = research.get("analysis_summary", self._build_analysis_summary(req, research))
        artifact["analysis_packet"] = research["analysis_packet"]
        artifact["quality_assessment"] = research.get("quality_assessment", {})
        artifact["quality_gate"] = {"status": "pending", "missing_modules": [], "quality_issues": []}
        return artifact

    def _run_reviewer(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        draft_content = self._read_text_file(artifact.get("spec_path", ""))
        quality_assessment = artifact.get("quality_assessment", {}) or {}

        if quality_assessment.get("decision") == "failed":
            raise DraftQualityError(
                "初稿未通过内容质量门禁，请先修复竞品分析质量问题。",
                artifact=artifact,
                issues=quality_assessment.get("blocking_issues", []),
                stage="quality_guardian",
            )

        if artifact.get("draft_type") == "detail":
            review_notes = []
            pending_questions = []
            if artifact.get("ticket_summary", {}).get("matched_ticket_count", 0) == 0:
                review_notes.append("未命中相关工单，详细需求场景证据不足。")
                pending_questions.append("需人工补充相关工单样本。")
            if not artifact.get("customer_profiles"):
                review_notes.append("缺少客户业务画像，需人工补充。")
            if not artifact.get("solution_candidates"):
                review_notes.append("缺少方案候选项。")
                pending_questions.append("需人工确认方案方向。")
            if artifact.get("revision_comments") and "## 本轮审核意见与修订响应" not in draft_content:
                review_notes.append("缺少本轮审核意见响应。")
            if not review_notes:
                review_notes.append("详细需求初稿基础结构完整，可继续人工深加工。")
            artifact["quality_gate"] = {
                "status": "passed",
                "missing_modules": [],
                "quality_issues": [],
                "section_statuses": {},
            }
            return {
                "review_notes": review_notes,
                "pending_questions": pending_questions,
                "summary": review_notes[0],
            }

        review_notes = []
        pending_questions = []
        quality_issues = []
        missing_modules = []
        section_statuses = {}

        requirements = {
            "业务场景拆解": bool(artifact.get("business_scenarios")),
            "客户业务特征分析": bool(artifact.get("customer_profiles")),
            "现有能力与缺口分析": bool(artifact.get("capability_analysis", {}).get("current_capabilities")),
            "功能改动点与影响面": bool(artifact.get("change_impact")),
            "方案与功能架构参考": bool(artifact.get("solution_candidates")) and bool(artifact.get("functional_architecture")),
            "工单全量附录": "## 工单全量附录" in draft_content and bool(artifact.get("ticket_appendix")),
        }
        for section, passed in requirements.items():
            section_statuses[section] = "completed" if passed else "failed"
            if not passed:
                missing_modules.append(section)

        if artifact.get("ticket_summary", {}).get("matched_ticket_count", 0) == 0:
            quality_issues.append("未命中相关工单，场景分析证据不足。")
            pending_questions.append("需人工补充相关工单样本。")

        placeholder_markers = [
            "待补充",
            "需人工补充功能边界",
            "当前未形成可用",
            "暂无明显客户画像",
        ]
        if any(marker in draft_content for marker in placeholder_markers):
            quality_issues.append("初稿仍包含明显占位内容，未形成可评审分析。")

        analysis_summary = artifact.get("analysis_summary", {}) or {}
        impact_surfaces = {item.get("surface", "") for item in artifact.get("change_impact", [])}
        for required_surface in analysis_summary.get("primary_surfaces", []) or []:
            if required_surface and required_surface not in impact_surfaces:
                missing_modules.append(f"{required_surface} 改动点分析")
                quality_issues.append(f"缺少 {required_surface} 改动点分析。")

        if artifact.get("revision_comments") and "## 本轮审核意见与修订响应" not in draft_content:
            quality_issues.append("缺少本轮审核意见响应。")

        missing_modules = list(dict.fromkeys(missing_modules))
        quality_issues = list(dict.fromkeys(quality_issues))
        if missing_modules or quality_issues:
            artifact["quality_gate"] = {
                "status": "failed",
                "missing_modules": missing_modules,
                "quality_issues": quality_issues,
                "section_statuses": section_statuses,
            }
            raise DraftQualityError(
                "初稿未通过质量门禁，请补齐缺失模块后重试。",
                artifact=artifact,
                issues=missing_modules + quality_issues,
            )

        review_notes.append("初稿已通过质量门禁，业务场景、客户分析、能力分析、改动点和方案架构参考完整。")
        artifact["quality_gate"] = {
            "status": "passed",
            "missing_modules": [],
            "quality_issues": [],
            "section_statuses": section_statuses,
        }
        return {
            "review_notes": review_notes,
            "pending_questions": pending_questions,
            "summary": review_notes[0],
        }

    def _persist_artifact(self, artifact: Dict[str, Any]):
        self._write_artifact_file(self._normalize_artifact_record(artifact))

    def _sync_requirement_with_draft(self, req_id: str, status: str, artifact: Dict[str, Any]):
        if not self.vector_store:
            return
        req = self.vector_store.get_requirement(req_id)
        if not req:
            return
        ai_analysis = dict(req.get("ai_analysis", {}) or {})
        ai_analysis["latest_draft_id"] = artifact.get("draft_id")
        ai_analysis["latest_draft_type"] = artifact.get("draft_type")
        ai_analysis["latest_draft_status"] = artifact.get("draft_status", status)
        ai_analysis["latest_run_id"] = artifact.get("latest_run_id") or artifact.get("draft_id")
        ai_analysis["latest_run_status"] = artifact.get("latest_run_status") or artifact.get("run_status", "completed")
        ai_analysis["pending_overwrite_draft_id"] = artifact.get("pending_overwrite_draft_id", "")
        ai_analysis["submitted_to_planning"] = artifact.get("submitted_to_planning", False)
        metadata = self._build_requirement_metadata(
            req,
            {
                "status": status,
                "ai_analysis": ai_analysis,
                "updated_at": datetime.now().isoformat(),
            },
        )
        self.vector_store.upsert_requirement(req_id, req.get("title", ""), req.get("description", ""), metadata)

    def _set_requirement_status(self, req_id: str, status: str):
        if not self.vector_store:
            return
        req = self.vector_store.get_requirement(req_id)
        if not req:
            return
        metadata = self._build_requirement_metadata(
            req,
            {
                "status": status,
                "updated_at": datetime.now().isoformat(),
            },
        )
        self.vector_store.upsert_requirement(req_id, req.get("title", ""), req.get("description", ""), metadata)

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.tasks.get(task_id)

    def list_drafts(self, req_id: str) -> Dict[str, Any]:
        drafts = self._load_draft_records(req_id)
        current = next((item for item in drafts if item.get("is_current")), None) or (drafts[0] if drafts else None)
        pending = next((item for item in drafts if item.get("overwrite_confirmation_required")), None)
        return {
            "current_draft": current,
            "runs": drafts,
            "pending_overwrite_candidate": pending,
        }

    def confirm_overwrite(self, req_id: str, draft_id: str) -> Dict[str, Any]:
        drafts = self._load_draft_records(req_id)
        target = next((item for item in drafts if item.get("draft_id") == draft_id), None)
        if target is None:
            raise ValueError("Draft artifact not found")
        for item in drafts:
            item["is_current"] = item.get("draft_id") == draft_id
            item["overwrite_confirmation_required"] = False
            self._write_artifact_file(item)
        self._sync_requirement_after_runs(req_id)
        return next((item for item in self._load_draft_records(req_id) if item.get("draft_id") == draft_id), target)

    def select_solution(self, req_id: str, draft_id: str, solution_index: int, notes: str = "") -> Dict[str, Any]:
        """用户选择初稿推荐方案（PRD 生成前必须选择）。"""
        artifact = self._read_artifact(draft_id)
        if artifact is None or artifact.get("req_id") != req_id:
            raise ValueError("Draft artifact not found")
        candidates = artifact.get("solution_candidates", [])
        if solution_index < 0 or solution_index >= len(candidates):
            raise ValueError(f"方案索引 {solution_index} 无效，共 {len(candidates)} 个候选方案")
        selected = candidates[solution_index]
        artifact["selected_solution"] = {
            "index": solution_index,
            "title": selected.get("title", ""),
            "notes": notes,
            "selected_at": datetime.now().isoformat(),
        }
        self._write_artifact_file(artifact)
        logger.info("[ReqPoolDraft] 方案已选择: req=%s draft=%s solution=%s", req_id, draft_id, selected.get("title"))
        return artifact["selected_solution"]

    def mark_draft_ready(self, req_id: str, draft_id: str, operator: str) -> Dict[str, Any]:
        artifact = self._read_artifact(draft_id)
        if artifact is None or artifact.get("req_id") != req_id:
            raise ValueError("Draft artifact not found")
        # 方案选择门控：多方案时必须先选择
        if not artifact.get("selected_solution"):
            candidates = artifact.get("solution_candidates", [])
            if len(candidates) > 1:
                raise ValueError("请先选择推荐方案后再标记就绪（在方案列表中点击选择）")
            elif len(candidates) == 1:
                artifact["selected_solution"] = {
                    "index": 0, "title": candidates[0].get("title", ""),
                    "notes": "唯一方案自动选择", "selected_at": datetime.now().isoformat(),
                }
        artifact["ready_to_submit"] = True
        artifact["ready_by"] = operator
        artifact["ready_at"] = datetime.now().isoformat()
        artifact["draft_status"] = "draft_ready"
        self._persist_artifact(artifact)
        self._sync_requirement_with_draft(req_id, "draft_ready", artifact)
        return artifact

    def submit_draft_to_planning(self, req_id: str, draft_id: str, submitted_by: str) -> Dict[str, Any]:
        artifact = self._read_artifact(draft_id)
        if artifact is None or artifact.get("req_id") != req_id:
            raise ValueError("Draft artifact not found")
        if not artifact.get("ready_to_submit"):
            raise ValueError("Draft must be marked ready before submit")
        artifact["submitted_to_planning"] = True
        artifact["submitted_by"] = submitted_by
        artifact["submitted_at"] = datetime.now().isoformat()
        artifact["draft_status"] = "scheduled"
        self._persist_artifact(artifact)
        self._sync_requirement_with_draft(req_id, "scheduled", artifact)
        return artifact

    def get_context(self, draft_id: str) -> Optional[Dict[str, Any]]:
        artifact = self._read_artifact(draft_id)
        if not artifact or not artifact.get("submitted_to_planning"):
            return None

        return {
            "draft_id": draft_id,
            "req_id": artifact.get("req_id"),
            "draft_type": artifact.get("draft_type"),
            "spec_file": artifact.get("spec_file"),
            "draft_content": self._read_text_file(artifact.get("spec_path", "")),
            "module_hint": artifact.get("module_hint", ""),
            "topic_names": artifact.get("topic_names", []),
            "ticket_summary": artifact.get("ticket_summary", {}),
            "ticket_appendix": artifact.get("ticket_appendix", []),
            "customer_profiles": artifact.get("customer_profiles", []),
            "business_scenarios": artifact.get("business_scenarios", []),
            "capability_analysis": artifact.get("capability_analysis", {}),
            "upstream_downstream_analysis": artifact.get("upstream_downstream_analysis", []),
            "change_impact": artifact.get("change_impact", []),
            "functional_architecture": artifact.get("functional_architecture", []),
            "evidence_bundle": artifact.get("evidence_bundle", {}),
            "risk_summary": artifact.get("risk_summary", []),
            "effort_estimate_size": artifact.get("effort_estimate_size", ""),
            "solution_candidates": artifact.get("solution_candidates", []),
            "problem_essence": artifact.get("problem_essence", "") or (artifact.get("analysis_packet", {}) or {}).get("problem_essence", ""),
            "requirement_type": artifact.get("requirement_type", "") or (artifact.get("analysis_packet", {}) or {}).get("requirement_type", ""),
            "module_candidates": artifact.get("module_candidates", []) or (artifact.get("analysis_packet", {}) or {}).get("module_candidates", []),
            "solution_generation_status": artifact.get("solution_generation_status", "") or (artifact.get("analysis_packet", {}) or {}).get("solution_generation_status", ""),
            "competitor_comparison": artifact.get("competitor_comparison", {}) or (artifact.get("analysis_packet", {}) or {}).get("competitor_comparison", {}),
            "design_fact_bundle": artifact.get("design_fact_bundle", {}) or (artifact.get("analysis_packet", {}) or {}).get("design_fact_bundle", {}),
            "source_citations": artifact.get("source_citations", []),
            "analysis_summary": artifact.get("analysis_summary", {}) or (artifact.get("analysis_packet", {}) or {}).get("analysis_summary", {}),
            "analysis_packet": artifact.get("analysis_packet", {}),
            "quality_assessment": artifact.get("quality_assessment", {}),
            "quality_gate": artifact.get("quality_gate", {}),
            "pending_questions": artifact.get("pending_questions", []),
            "review_notes": artifact.get("review_notes", []),
            "related_requirements": artifact.get("related_requirements", []),
            "source": artifact.get("source", "req_pool_mission_control"),
            "title": artifact.get("title", ""),
            "created_at": artifact.get("created_at", ""),
            "version": artifact.get("version", 1),
            "revision_comments": artifact.get("revision_comments", ""),
            "revision_history": artifact.get("revision_history", []),
            "revision_iteration": artifact.get("revision_iteration", 0),
            "section_diffs": artifact.get("section_diffs", {}),
            "submitted_to_planning": artifact.get("submitted_to_planning", False),
            "submitted_by": artifact.get("submitted_by", ""),
            "submitted_at": artifact.get("submitted_at", ""),
        }


# Module-level registry
_draft_service_instance = None


def register_draft_service(svc: "ReqPoolDraftService") -> None:
    global _draft_service_instance
    _draft_service_instance = svc


def get_draft_service() -> "ReqPoolDraftService":
    return _draft_service_instance
