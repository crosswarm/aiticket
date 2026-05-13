"""
requirements_pool_aggregate_service.py — 需求池多智能体聚合分析流水线编排

流水线阶段：
  Stage 1  Researcher            req_cluster_agent   — 聚类 + 价值评分 + 场景收敛
  Stage 2  Explorer              req_enricher_agent  — per-cluster 事实取证（并行 fan-out）
  Stage 3  PRDDever·MC (Atlas)   req_analyst_agent   — per-req 收集拆解调研（Mission Control）
  Stage 4  PRDMaster (Victor)    req_solution_agent  — per-cluster 候选方案 + 评审（基于调研决策）
            注：Stage 4 运行时 agent_name='req_solution'（Sage 化身），Victor 身份已在 identity 层归并，
            agent_tasks 历史记录 req_solution/prd_master 两个 name 均属 Victor 的工作记录。
  Stage 5  Reviewer              claude_agent        — 跨主题合并评审

状态契约：req.status 在 Stage 4 PRDDever 成功写出 ai_analysis 之前保持 'new'。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_FAN_OUT_TIMEOUT = 300   # 每个 per-cluster sub-agent 最长等待秒数
_ANALYST_TIMEOUT = 180   # 每条 req analyst 最长等待秒数


class PipelineCancelled(Exception):
    """流水线收到协作式取消信号时抛出"""
    pass


class RequirementsPoolAggregateService:
    _instance: Optional["RequirementsPoolAggregateService"] = None

    def __init__(self, vector_store=None, req_pool_service=None):
        self._vs = vector_store
        self._rps = req_pool_service
        self._cancel_flags: dict = {}   # parent_id → True 表示请求取消

    @classmethod
    def get_instance(cls) -> "RequirementsPoolAggregateService":
        if cls._instance is None:
            from vector_store import VectorStore
            vs = VectorStore.get_instance() if hasattr(VectorStore, "get_instance") else None
            cls._instance = cls(vector_store=vs)
        return cls._instance

    # ── 公共接口 ────────────────────────────────────────────────────────────
    def run_pipeline(self, scope: str = "all_new", req_ids: list = None, theme_hints: list = None,
                     auto_accept_themes: bool = False,
                     exclude_with_solution: bool = True) -> tuple:
        """
        启动完整聚合分析流水线，返回 (parent_task_id, excluded_count)。
        流水线异步运行，通过 /api/agent_tasks?parent= 轮询进度。

        exclude_with_solution: 排除已属于"已出方案"cluster 的 req_id
                              （cluster.solutions_ready / status='solutions_ready' / pm_approved 任一命中）。
        """
        from services.agent_task_store import AgentTaskStore, AgentStatus
        from agents.base import AgentTask

        eligible, excluded_count = self._get_eligible_req_ids(
            scope, req_ids, exclude_with_solution=exclude_with_solution
        )
        if not eligible:
            raise ValueError("没有可处理的新需求（status='new'）")

        store = AgentTaskStore.get_instance()
        parent_task = AgentTask.new(
            agent_name="requirement_aggregate_pipeline",
            title=f"需求池聚合分析（{len(eligible)} 条新需求）",
            trigger_src="aggregate_service",
            payload_json=json.dumps({"scope": scope, "eligible_count": len(eligible),
                                     "theme_hints": theme_hints or []}, ensure_ascii=False),
        )
        store.insert(parent_task)
        store.update_status(parent_task.id, AgentStatus.RUNNING, started_at=datetime.utcnow())

        t = threading.Thread(
            target=self._run_pipeline_sync,
            args=(parent_task.id, eligible, theme_hints or [], auto_accept_themes),
            daemon=True,
            name=f"agg-pipeline-{parent_task.id[-6:]}",
        )
        t.start()

        logger.info(f"[AggPipeline] 启动 parent_task={parent_task.id}，eligible={len(eligible)} 条，excluded={excluded_count} 条")
        return parent_task.id, excluded_count

    def get_pipeline_status(self, parent_task_id: str) -> dict:
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()
        parent = store.get(parent_task_id)
        if not parent:
            return {"error": "not_found"}
        children = store.list_children(parent_task_id)
        # 解析 result_json 判断是否处于主题确认等待状态
        pending_theme_confirm = None
        themes_meta = []
        try:
            result = json.loads(parent.result_json or "{}")
            themes_meta = result.get("themes", [])
            if result.get("pipeline_stage") == "waiting_theme_confirm":
                pending_theme_confirm = {
                    "themes": result.get("themes", []),
                    "cluster_ids": result.get("cluster_ids", []),
                    "pending_since": result.get("pending_since"),
                    "timeout_minutes": result.get("timeout_minutes", 60),
                }
        except Exception:
            pass
        return {
            "parent_task_id": parent_task_id,
            "overall_status": parent.status.value,
            "progress": parent.progress,
            "sub_tasks": [t.to_dict() for t in children],
            "log_tail": parent.log_tail,
            "pending_theme_confirm": pending_theme_confirm,
            "themes_meta": themes_meta,
        }

    def retry_stage(self, parent_task_id: str) -> bool:
        """重新触发整条流水线（简化版：重跑全量）"""
        logger.warning(f"[AggPipeline] retry_stage: parent={parent_task_id}（当前实现为重跑全量）")
        try:
            self.run_pipeline()  # 返回 tuple，retry 不关心
            return True
        except Exception as e:
            logger.error(f"[AggPipeline] retry_stage failed: {e}")
            return False

    def get_theme_stage_notes(self, parent_id: str):
        """读取主题×阶段留言 dict，找不到 parent 返回 None。"""
        from services.agent_task_store import AgentTaskStore
        task = AgentTaskStore.get_instance().get(parent_id)
        if not task:
            return None
        try:
            return json.loads(task.result_json or "{}").get("theme_stage_notes", {})
        except Exception:
            return {}

    def set_theme_stage_note(self, parent_id: str, theme_id: str, stage: str, note: str) -> tuple:
        """
        写入 (theme_id, stage) 留言。
        若对应子任务已不在 pending/queued 状态，返回 (False, reason)。
        """
        from services.agent_task_store import AgentTaskStore, AgentStatus
        store = AgentTaskStore.get_instance()
        parent = store.get(parent_id)
        if not parent:
            return False, "pipeline not found"
        for child in store.list_children(parent_id):
            if child.agent_name != stage:
                continue
            try:
                pl = json.loads(child.payload_json or "{}")
            except Exception:
                pl = {}
            if (pl.get("cluster_id") or pl.get("req_id", "")) == theme_id:
                if child.status.value not in ("pending", "queued"):
                    return False, "该阶段已开始，无法修改留言"
        with self._progress_lock:
            try:
                task = store.get(parent_id)
                if not task:
                    return False, "pipeline not found"
                result = json.loads(task.result_json or "{}")
                notes = result.get("theme_stage_notes", {})
                notes[f"{theme_id}:{stage}"] = note
                result["theme_stage_notes"] = notes
                store.update_status(parent_id, AgentStatus.RUNNING,
                                    result_json=json.dumps(result, ensure_ascii=False, default=str))
            except Exception as e:
                return False, str(e)
        return True, "ok"

    # ── 内部编排 ─────────────────────────────────────────────────────────────
    def _run_pipeline_sync(self, parent_id: str, eligible_req_ids: list, theme_hints: list = None,
                           auto_accept_themes: bool = False):
        from services.agent_task_store import AgentTaskStore, AgentStatus
        store = AgentTaskStore.get_instance()

        def _log(msg: str):
            store.append_log(parent_id, msg)
            logger.info(f"[AggPipeline:{parent_id[-6:]}] {msg}")

        def _set_progress(pct: int):
            store.update_status(parent_id, AgentStatus.RUNNING, progress=pct)

        # ── 本地模型 preflight（ensure SuperGemma4 在线后再跑 LLM 密集阶段）──
        try:
            from services.local_llm_lifecycle import with_fallback, shutdown_if_started_by_us
            _routed = with_fallback("req_pool_pipeline")
            _log(f"🤖 LLM 路由：{_routed}（SuperGemma4 preflight 完成）")
        except Exception as _pf_exc:
            _routed = "unknown"
            _log(f"⚠️ preflight 异常（{_pf_exc}），继续靠 LLMService 路由")
            shutdown_if_started_by_us = lambda _: None  # noqa: E731

        try:
            # ── Stage 1: Researcher ─────────────────────────────────────────
            self._check_cancel(parent_id, "Stage1", _log)
            _log("🗂️ Stage 1 Researcher 聚类开始")
            _set_progress(5)
            cluster_payload = {}
            if theme_hints:
                cluster_payload["theme_hints"] = theme_hints
                _log(f"💡 主题方向引导：{theme_hints}")
            cluster_result = self._run_agent_stage(
                parent_id, "req_cluster", "Stage1-Researcher-聚类", cluster_payload
            )
            if cluster_result.get("error"):
                raise RuntimeError(f"Stage 1 失败: {cluster_result['error']}")
            _log(f"✅ Stage 1 完成：clusters_total={cluster_result.get('clusters_total', '?')}")
            _set_progress(20)

            # 取所有 cluster_id
            cluster_ids = self._get_pipeline_cluster_ids(eligible_req_ids)
            if not cluster_ids:
                _log("⚠️ 未产生任何 cluster，流水线结束")
                store.update_status(parent_id, AgentStatus.SUCCEEDED,
                                    finished_at=datetime.utcnow(), progress=100)
                return

            _log(f"📦 发现 {len(cluster_ids)} 个 cluster，等待用户主题确认（最长 60 分钟）")

            # ── 主题确认闸门（双闸门第二段：聚类后硬确认）──────────────────
            cluster_ids = self._wait_for_theme_confirm(
                parent_id, cluster_ids, store, _log, auto_accept_themes=auto_accept_themes
            )
            _log(f"✅ 主题确认完成：最终 {len(cluster_ids)} 个 cluster 进入加工")

            # ── Stage 2-4: Per-theme 独立流水线（每个 cluster 自行 2→3→4）──
            self._check_cancel(parent_id, "PerThemeStages", _log)
            _set_progress(25)
            payloads_map = self._build_analyst_payloads_map(cluster_ids, eligible_req_ids)
            total_reqs = sum(len(v) for v in payloads_map.values())
            _log(f"🚀 启动 per-theme 流水线：{len(cluster_ids)} 个 cluster，共 {total_reqs} 条 req，各自独立跑 Stage 2→3→4")
            theme_results = self._run_per_theme_stages(parent_id, cluster_ids, payloads_map,
                                                        timeout_per_cluster=1200)
            done_count = sum(1 for r in theme_results.values() if not r.get("error"))
            fail_count = len(theme_results) - done_count
            _log(f"✅ Per-theme 阶段完成：成功 {done_count} 个 cluster"
                 + (f"，失败 {fail_count} 个（见各子任务日志）" if fail_count else ""))
            _set_progress(90)

            # ── Stage 5: Reviewer ───────────────────────────────────────────
            self._check_cancel(parent_id, "Stage5", _log)
            _log("🔍 Stage 5 Reviewer 跨主题评审")
            self._run_agent_stage(
                parent_id, "claude", "Stage5-Reviewer-合并评审",
                {"mode": "cross_theme_review", "parent_task_id": parent_id}
            )
            _log("✅ Stage 5 完成，流水线全部结束")

            self._cancel_flags.pop(parent_id, None)
            store.update_status(parent_id, AgentStatus.SUCCEEDED,
                                finished_at=datetime.utcnow(), progress=100)

        except PipelineCancelled:
            # 协作式取消正常路径，不记为失败
            self._cancel_flags.pop(parent_id, None)
            store.update_status(parent_id, AgentStatus.CANCELLED, finished_at=datetime.utcnow())
        except Exception as exc:
            logger.error(f"[AggPipeline:{parent_id[-6:]}] 流水线失败: {exc}", exc_info=True)
            store.update_status(parent_id, AgentStatus.FAILED,
                                finished_at=datetime.utcnow(),
                                result_json=json.dumps({"error": str(exc)}, ensure_ascii=False))
            store.append_log(parent_id, f"❌ 流水线失败: {exc}")
        finally:
            # 随手关灯：仅关闭由本流水线 preflight 启动的 SuperGemma4
            try:
                shutdown_if_started_by_us("req_pool_pipeline")
            except Exception as _sd_exc:
                logger.debug("[AggPipeline] shutdown_if_started_by_us 异常（可忽略）: %s", _sd_exc)

    def _run_agent_stage(self, parent_id: str, agent_name: str, title: str, payload: dict) -> dict:
        """同步运行单个 agent stage，创建 task 并等待返回。"""
        from services.agent_task_store import AgentTaskStore, AgentStatus
        from agents.base import AgentTask
        from agents.registry import AgentRegistry

        store = AgentTaskStore.get_instance()

        # 注入用户留言（theme × stage 维度）
        try:
            _par = store.get(parent_id)
            if _par:
                _notes = json.loads(_par.result_json or "{}").get("theme_stage_notes", {})
                _cid = payload.get("cluster_id") or payload.get("req_id", "")
                if _cid:
                    _directive = _notes.get(f"{_cid}:{agent_name}", "")
                    if _directive:
                        payload = dict(payload)
                        payload["extra_user_directives"] = _directive
        except Exception:
            pass

        task = AgentTask.new(
            agent_name=agent_name,
            title=title,
            trigger_src=f"pipeline:{parent_id}",
            payload_json=json.dumps(payload, ensure_ascii=False),
            parent_id=parent_id,
        )
        store.insert(task)
        store.update_status(task.id, AgentStatus.RUNNING, started_at=datetime.utcnow())

        try:
            agent = AgentRegistry.get_instance().get(agent_name)
            if agent is None:
                result = {"error": f"agent '{agent_name}' not registered"}
            else:
                result = agent.run_task(task) or {}
            store.update_status(task.id, AgentStatus.SUCCEEDED,
                                finished_at=datetime.utcnow(), progress=100,
                                result_json=json.dumps(result, ensure_ascii=False, default=str))
        except Exception as exc:
            result = {"error": str(exc)}
            store.update_status(task.id, AgentStatus.FAILED,
                                finished_at=datetime.utcnow(),
                                result_json=json.dumps(result, ensure_ascii=False))
        return result

    def _run_fan_out(self, parent_id: str, agent_name: str, title_prefix: str,
                     payloads: list, timeout: int = 300):
        """并行对多个 payload 运行同一 agent，等待全部完成。"""
        results = {}
        errors = {}
        lock = threading.Lock()
        total = len(payloads)
        # 计数器：已完成数量
        progress_counter = {"done": 0}

        def _worker(idx: int, payload: dict):
            cid = payload.get("cluster_id") or payload.get("req_id") or ""
            cluster_label = self._safe_cluster_label(cid) if payload.get("cluster_id") else ""
            # 处理 start
            self._emit_progress(
                parent_id,
                stage=title_prefix,
                current_cluster_id=cid,
                current_cluster_label=cluster_label,
                current_agent=agent_name,
                processed=idx,
                total=total,
                current_action="开始处理",
            )
            result = self._run_agent_stage(
                parent_id, agent_name,
                f"{title_prefix}[{idx + 1}/{total}]",
                payload,
            )
            with lock:
                if result.get("error"):
                    errors[idx] = result["error"]
                else:
                    results[idx] = result
                progress_counter["done"] += 1
                done_now = progress_counter["done"]
            # 处理 end（用 done_now 反映"已完成数"）
            self._emit_progress(
                parent_id,
                stage=title_prefix,
                current_cluster_id=cid,
                current_cluster_label=cluster_label,
                current_agent=agent_name,
                processed=done_now,
                total=total,
                current_action="完成" if not result.get("error") else "失败",
            )

        threads = [threading.Thread(target=_worker, args=(i, p), daemon=True)
                   for i, p in enumerate(payloads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout)

        if errors:
            logger.warning(f"[AggPipeline] {title_prefix} 部分失败: {errors}")
        return results

    # ── 进度上报 ──────────────────────────────────────────────────────────────
    _progress_lock = threading.Lock()

    def _safe_cluster_label(self, cluster_id: str) -> str:
        """根据 cluster_id 拿 label，失败返回空串。"""
        if not cluster_id:
            return ""
        try:
            vs = self._get_vs()
            cluster = vs.get_cluster(cluster_id)
            if not cluster:
                return ""
            l1 = cluster.get("topic_l1", "")
            l2 = cluster.get("topic_l2", "")
            try:
                from services.topic_codec import resolve_topic_label as _resolve_label
                lab = _resolve_label(l1, l2)
                return lab.get("display") or (l1 + (" · " + l2 if l2 else ""))
            except Exception:
                return l1 + (" · " + l2 if l2 else "")
        except Exception:
            return ""

    def _emit_progress(self, parent_id: str,
                        stage: str = "",
                        current_cluster_id: str = "",
                        current_cluster_label: str = "",
                        current_agent: str = "",
                        processed: int = 0,
                        total: int = 0,
                        current_action: str = ""):
        """
        合并写入 parent task result_json.pipeline_progress 子字段，
        不覆盖 pipeline_stage/themes/cluster_ids 等其它键。
        """
        from services.agent_task_store import AgentTaskStore, AgentStatus
        store = AgentTaskStore.get_instance()
        with self._progress_lock:
            try:
                task = store.get(parent_id)
                if not task:
                    return
                try:
                    result = json.loads(task.result_json or "{}")
                except Exception:
                    result = {}
                progress = dict(result.get("pipeline_progress") or {})
                progress.update({
                    "stage": stage,
                    "current_cluster_id": current_cluster_id,
                    "current_cluster_label": current_cluster_label,
                    "current_agent": current_agent,
                    "processed": processed,
                    "total": total,
                    "current_action": current_action,
                    "updated_at": datetime.utcnow().isoformat(),
                })
                result["pipeline_progress"] = progress
                store.update_status(parent_id, AgentStatus.RUNNING,
                                    result_json=json.dumps(result, ensure_ascii=False, default=str))
            except Exception as e:
                logger.warning(f"[AggPipeline] _emit_progress failed: {e}")

    # ── 辅助方法 ──────────────────────────────────────────────────────────────
    def _get_eligible_req_ids(self, scope: str, req_ids: list,
                              exclude_with_solution: bool = True) -> tuple:
        """
        返回 (eligible_req_ids, excluded_count)。
        exclude_with_solution=True 时，从 eligible 列表中剔除已属于『已出方案』cluster 的 req_id。
        判定规则：cluster 满足 solutions_ready=True OR status='solutions_ready' OR pm_approved=True 任一即视为『已出方案』。
        """
        vs = self._get_vs()
        all_reqs = vs.list_requirements(status="new")
        if scope == "selected" and req_ids:
            eligible = [r["req_id"] for r in all_reqs if r["req_id"] in req_ids]
        else:
            eligible = [r["req_id"] for r in all_reqs]

        if not exclude_with_solution:
            return eligible, 0

        # 收集已有方案 cluster 的成员 req_id
        excluded_member_ids: set = set()
        try:
            clusters = vs.list_clusters()
        except Exception as e:
            logger.warning(f"[AggPipeline] list_clusters failed during exclude: {e}")
            clusters = []

        for cluster in clusters:
            sol_ready = cluster.get("solutions_ready")
            cstatus = cluster.get("status", "")
            pm_ok = cluster.get("pm_approved")
            # 兼容字符串 'true'/'True' 与布尔 True
            def _truthy(v):
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.lower() == "true"
                return bool(v)
            if _truthy(sol_ready) or cstatus == "solutions_ready" or _truthy(pm_ok):
                raw_members = cluster.get("member_req_ids", "[]")
                try:
                    members = json.loads(raw_members) if isinstance(raw_members, str) else (raw_members or [])
                except Exception:
                    members = []
                excluded_member_ids.update(members)

        before = len(eligible)
        eligible = [rid for rid in eligible if rid not in excluded_member_ids]
        excluded_count = before - len(eligible)
        if excluded_count:
            logger.info(f"[AggPipeline] exclude_with_solution 剔除 {excluded_count} 条 req（来自已出方案 cluster）")
        return eligible, excluded_count

    def _get_pipeline_cluster_ids(self, eligible_req_ids: list) -> list:
        vs = self._get_vs()
        clusters = vs.list_clusters()
        return [c["cluster_id"] for c in clusters if c.get("cluster_id")]

    def _build_analyst_payloads(self, cluster_ids: list, eligible_req_ids: list) -> list:
        vs = self._get_vs()
        payloads = []
        eligible_set = set(eligible_req_ids)
        for cid in cluster_ids:
            cluster = vs.get_cluster(cid)
            if not cluster:
                continue
            member_ids = json.loads(cluster.get("member_req_ids", "[]"))
            for req_id in member_ids:
                if req_id in eligible_set:
                    payloads.append({"req_id": req_id, "cluster_id": cid})
        return payloads

    def _build_analyst_payloads_map(self, cluster_ids: list, eligible_req_ids: list) -> dict:
        """{cluster_id: [req_id, ...]}，per-cluster pipeline 自己消费。"""
        vs = self._get_vs()
        eligible_set = set(eligible_req_ids)
        out: dict = {cid: [] for cid in cluster_ids}
        for cid in cluster_ids:
            cluster = vs.get_cluster(cid)
            if not cluster:
                continue
            member_ids = json.loads(cluster.get("member_req_ids", "[]"))
            out[cid] = [rid for rid in member_ids if rid in eligible_set]
        return out

    def _run_cluster_pipeline(self, parent_id: str, cid: str, member_req_ids: list) -> dict:
        """单个 cluster 串行跑 Stage 2 → 3 → 4，不依赖全局屏障。"""
        try:
            r2 = self._run_agent_stage(parent_id, "req_enricher",
                                       f"Stage2-Explorer-取证[{cid[:8]}]",
                                       {"cluster_id": cid})
            if r2.get("error"):
                return {"cluster_id": cid, "stopped_at": "stage2", "error": r2["error"]}

            import concurrent.futures as _cf3
            for req_id in member_req_ids:
                def _analyst_task(_rid=req_id):
                    return self._run_agent_stage(parent_id, "req_analyst",
                                                 f"Stage3-PRDDever-MC[{cid[:8]}/{_rid[:8]}]",
                                                 {"req_id": _rid, "cluster_id": cid})
                _ex3 = _cf3.ThreadPoolExecutor(max_workers=1)
                _fut3 = _ex3.submit(_analyst_task)
                try:
                    r3 = _fut3.result(timeout=120)
                except _cf3.TimeoutError:
                    r3 = {"error": "analyst timeout 120s"}
                    logger.warning(f"[AggPipeline] cluster={cid[:8]} req={req_id[:8]} Stage3 超时 120s")
                finally:
                    _ex3.shutdown(wait=False)  # don't block on slow/stuck LLM thread
                if r3.get("error"):
                    logger.warning(f"[AggPipeline] cluster={cid[:8]} req={req_id[:8]} Stage3 失败: {r3['error']}")

            import concurrent.futures as _cf4
            _ex4 = _cf4.ThreadPoolExecutor(max_workers=1)
            _fut4 = _ex4.submit(lambda: self._run_agent_stage(
                parent_id, "req_solution",
                f"Stage4-PRDMaster-方案[{cid[:8]}]",
                {"cluster_id": cid}))
            try:
                r4 = _fut4.result(timeout=300)
            except _cf4.TimeoutError:
                r4 = {"error": "solution timeout 300s"}
                logger.warning(f"[AggPipeline] cluster={cid[:8]} Stage4 超时 300s")
            finally:
                _ex4.shutdown(wait=False)
            if r4.get("error"):
                return {"cluster_id": cid, "stopped_at": "stage4", "error": r4["error"]}

            return {"cluster_id": cid, "stopped_at": "done"}
        except Exception as exc:
            logger.exception(f"[AggPipeline] cluster={cid[:8]} pipeline 崩溃: {exc}")
            return {"cluster_id": cid, "stopped_at": "exception", "error": str(exc)}

    def _mark_dangling_tasks_failed(self, parent_id: str, thread_name: str,
                                     timeout_s: int = 1200) -> None:
        """超时仍在 RUNNING 的子任务强制标 FAILED，避免前端永显进行中。
        timeout_s=0 时忽略时间检查，直接收尸所有 RUNNING task（末尾兜底用）。
        """
        from services.agent_task_store import AgentTaskStore, AgentStatus
        store = AgentTaskStore.get_instance()
        now = datetime.utcnow()
        for c in store.list_children(parent_id):
            if c.status != AgentStatus.RUNNING:
                continue
            if c.agent_name not in ("req_enricher", "req_analyst", "req_solution"):
                continue
            # 防误伤：timeout_s>0 时只收尸启动超过 timeout_s 秒的 task
            if timeout_s > 0 and c.started_at:
                try:
                    age_s = (now - c.started_at.replace(tzinfo=None)).total_seconds()
                    if age_s < timeout_s:
                        continue
                except Exception:
                    pass
            cid = ""
            try:
                cid = json.loads(c.payload_json or "{}").get("cluster_id", "")
            except Exception:
                pass
            store.update_status(
                c.id, AgentStatus.FAILED,
                finished_at=now,
                result_json=json.dumps(
                    {"error": f"超过 {timeout_s}s 仍未完成，被流水线收尸",
                     "thread_name": thread_name, "cluster_id": cid},
                    ensure_ascii=False,
                ),
            )
            logger.warning(f"[AggPipeline] 收尸 task={c.id[:12]} cid={cid[:8]} thread={thread_name}")

    def _run_per_theme_stages(self, parent_id: str, cluster_ids: list,
                               payloads_map: dict, timeout_per_cluster: int = 1200) -> dict:
        """每个 cluster 独立一条线程跑完 Stage 2→3→4，互不阻塞。"""
        results: dict = {}
        lock = threading.Lock()
        # 最多 4 个 cluster 同时进 LLM，防 rate-limit miss（借鉴 multica max_concurrent_tasks）
        _sem = threading.Semaphore(4)

        def _worker(cid: str):
            with _sem:
                r = self._run_cluster_pipeline(parent_id, cid, payloads_map.get(cid, []))
            with lock:
                results[cid] = r

        threads = [
            threading.Thread(target=_worker, args=(cid,),
                             daemon=True, name=f"reqpool-pipe-{cid[:8]}")
            for cid in cluster_ids
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout_per_cluster)
            if t.is_alive():
                logger.warning(f"[AggPipeline] {t.name} 超过 {timeout_per_cluster}s 未结束，收尸")
                self._mark_dangling_tasks_failed(parent_id, t.name, timeout_per_cluster)

        # 末尾兜底：把所有仍在 RUNNING 且超过 60s 未收尾的子任务清掉
        self._mark_dangling_tasks_failed(parent_id, "final-sweep", timeout_s=60)

        return results

    def kickoff_single_cluster(self, cluster_id: str) -> str:
        """重跑单个 cluster 的 Stage 2→3→4，返回新建的 parent_task_id。"""
        from services.agent_task_store import AgentTaskStore, AgentStatus
        from agents.base import AgentTask

        store = AgentTaskStore.get_instance()
        vs = self._get_vs()
        cluster = vs.get_cluster(cluster_id)
        label = self._safe_cluster_label(cluster_id) if cluster else cluster_id[:8]

        parent_task = AgentTask.new(
            agent_name="requirement_aggregate_pipeline_single",
            title=f"单主题重跑：{label}",
            trigger_src="api:rerun_cluster",
            payload_json=json.dumps({"cluster_id": cluster_id}, ensure_ascii=False),
        )
        store.insert(parent_task)
        store.update_status(parent_task.id, AgentStatus.RUNNING, started_at=datetime.utcnow())

        member_req_ids: list = []
        if cluster:
            try:
                member_req_ids = json.loads(cluster.get("member_req_ids", "[]"))
            except Exception:
                pass

        def _run():
            try:
                result = self._run_cluster_pipeline(parent_task.id, cluster_id, member_req_ids)
                status = AgentStatus.FAILED if result.get("error") else AgentStatus.SUCCEEDED
                store.update_status(parent_task.id, status,
                                    finished_at=datetime.utcnow(), progress=100,
                                    result_json=json.dumps(result, ensure_ascii=False, default=str))
            except Exception as exc:
                store.update_status(parent_task.id, AgentStatus.FAILED,
                                    finished_at=datetime.utcnow(),
                                    result_json=json.dumps({"error": str(exc)}, ensure_ascii=False))

        threading.Thread(target=_run, daemon=True, name=f"reqpool-single-{cluster_id[:8]}").start()
        return parent_task.id

    def _get_vs(self):
        if self._vs:
            return self._vs
        from vector_store import VectorStore
        vs = VectorStore(persist_directory="chroma_db", allow_download=False)
        self._vs = vs
        return vs

    # ── 主题确认闸门 ──────────────────────────────────────────────────────────
    _THEME_CONFIRM_POLL_INTERVAL = 5    # 秒
    _THEME_CONFIRM_TIMEOUT = 60 * 60    # 60 分钟，超时自动 accept_all

    def _wait_for_theme_confirm(self, parent_id: str, cluster_ids: list,
                                 store, _log, auto_accept_themes: bool = False) -> list:
        """
        写入 waiting_theme_confirm 状态，轮询等待 API 写入 theme_confirmed。
        auto_accept_themes=True 时立即跳过确认门（定时任务无人值守场景）。
        60 分钟超时自动 accept_all（不阻塞定时任务）。
        返回最终确认的 cluster_id 列表。
        """
        if auto_accept_themes:
            _log(f"⚡ auto_accept_themes=True，跳过主题确认门，直接接受全部 {len(cluster_ids)} 个 cluster")
            return cluster_ids
        import time
        from services.agent_task_store import AgentStatus

        # 构建主题元数据（用于前端展示）
        vs = self._get_vs()
        try:
            from services.topic_codec import resolve_topic_label as _resolve_label
        except Exception:
            _resolve_label = None
        themes_meta = []
        for cid in cluster_ids:
            cluster = vs.get_cluster(cid)
            if cluster:
                l1 = cluster.get("topic_l1", "")
                l2 = cluster.get("topic_l2", "")
                if _resolve_label:
                    label = _resolve_label(l1, l2)
                    l1_name = label["l1_name"]
                    l2_name = label["l2_name"]
                    display = label["display"]
                else:
                    l1_name, l2_name, display = l1, l2, (l1 + (" · " + l2 if l2 else ""))
                themes_meta.append({
                    "cluster_id": cid,
                    "topic_l1": l1,
                    "topic_l2": l2,
                    "topic_l1_name": l1_name,
                    "topic_l2_name": l2_name,
                    "topic_display": display,
                    "ticket_count": cluster.get("ticket_count", 0),
                    "value_score": cluster.get("value_score", 0),
                })

        waiting_payload = json.dumps({
            "pipeline_stage": "waiting_theme_confirm",
            "cluster_ids": cluster_ids,
            "themes": themes_meta,
            "pending_since": datetime.utcnow().isoformat(),
            "timeout_minutes": 60,
        }, ensure_ascii=False)
        store.update_status(parent_id, AgentStatus.RUNNING, result_json=waiting_payload)
        _log(f"⏸️ 等待用户确认 {len(themes_meta)} 个主题（60 分钟超时自动接受全部）")

        deadline = time.time() + self._THEME_CONFIRM_TIMEOUT
        while time.time() < deadline:
            time.sleep(self._THEME_CONFIRM_POLL_INTERVAL)
            task = store.get(parent_id)
            if not task:
                break
            try:
                result = json.loads(task.result_json or "{}")
            except Exception:
                result = {}
            if result.get("pipeline_stage") == "theme_confirmed":
                keep = result.get("themes_keep", cluster_ids)
                drop = set(result.get("themes_drop", []))
                confirmed = [c for c in cluster_ids if c in set(keep) and c not in drop]
                _log(f"✅ 用户确认：保留 {len(confirmed)} 个主题，丢弃 {len(cluster_ids)-len(confirmed)} 个")
                return confirmed if confirmed else cluster_ids

        # 超时 → auto-accept all（保留 themes/notes 等已有字段）
        _log("⏰ 超时 60 分钟，自动接受全部主题继续流水线")
        try:
            _cur = store.get(parent_id)
            _cur_r = json.loads(_cur.result_json or "{}") if _cur else {}
        except Exception:
            _cur_r = {}
        _cur_r.update({"pipeline_stage": "pipeline_running", "cluster_ids": cluster_ids})
        store.update_status(parent_id, AgentStatus.RUNNING, result_json=json.dumps(_cur_r, ensure_ascii=False))
        return cluster_ids

    # ── 硬取消 + 清理 ─────────────────────────────────────────────────────────
    def cancel_and_cleanup(self, parent_id: str) -> dict:
        """
        硬取消流水线：
        1. 设置协作式取消信号（让 daemon 线程在下个 stage 边界退出）
        2. flip SQLite 状态 → CANCELLED
        3. 删除 ChromaDB 中本次产生的 cluster 行
        4. 将本次 cluster 覆盖的 req 中已变为 draft_review 的回滚到 new
        5. 返回清理摘要
        """
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()

        # 1. 设置取消信号（daemon 线程检查此 flag 提前退出）
        self._cancel_flags[parent_id] = True

        # 2. flip SQLite
        store.cancel(parent_id)
        children = store.list_children(parent_id)
        child_count = sum(1 for c in children if store.cancel(c.id))

        # 3. 从 result_json 取出本次流水线产生的 cluster_ids
        task = store.get(parent_id)
        cluster_ids: list = []
        if task:
            try:
                result = json.loads(task.result_json or "{}")
                cluster_ids = result.get("cluster_ids") or []
                # waiting_theme_confirm 场景
                if not cluster_ids and result.get("pipeline_stage") == "waiting_theme_confirm":
                    cluster_ids = result.get("cluster_ids", [])
            except Exception:
                pass

        # 如果 result_json 里没有，直接从 req_clusters 全量拿（只删 active 且本次产物）
        if not cluster_ids:
            vs = self._get_vs()
            cluster_ids = [c["cluster_id"] for c in vs.list_clusters(status="active")]

        # 4. 删除 cluster 行 + 回滚 req 状态
        vs = self._get_vs()
        cleaned_clusters: list = []
        rolled_back_reqs: list = []
        all_member_req_ids: set = set()

        for cid in cluster_ids:
            cluster = vs.get_cluster(cid)
            if cluster:
                try:
                    members = json.loads(cluster.get("member_req_ids", "[]"))
                    all_member_req_ids.update(members)
                except Exception:
                    pass
            if vs.delete_cluster(cid):
                cleaned_clusters.append(cid)
                logger.info(f"[AggPipeline:cancel] 删除 cluster {cid}")

        for req_id in all_member_req_ids:
            req = vs.get_requirement(req_id)
            if req and req.get("status") == "draft_review":
                ok = vs.update_requirement_field(req_id, {
                    "status": "new",
                    "ai_analysis": None,
                    "theme_context": None,
                })
                if ok:
                    rolled_back_reqs.append(req_id)
                    logger.info(f"[AggPipeline:cancel] 回滚 req {req_id} → new")

        logger.info(
            f"[AggPipeline:cancel] parent={parent_id[-8:]} | "
            f"clusters_cleaned={len(cleaned_clusters)} | reqs_rolled_back={len(rolled_back_reqs)}"
        )
        return {
            "cancelled": True,
            "parent": parent_id,
            "children_cancelled": child_count,
            "cleaned_clusters": len(cleaned_clusters),
            "rolled_back_reqs": rolled_back_reqs,
        }

    def _check_cancel(self, parent_id: str, stage_name: str, _log):
        """在每个 stage 进入前调用，若收到取消信号则 raise PipelineCancelled。"""
        if self._cancel_flags.get(parent_id):
            _log(f"🛑 [{stage_name}] 收到取消信号，提前退出")
            raise PipelineCancelled(f"cancelled before {stage_name}")

    def confirm_themes(self, parent_id: str, themes_keep: list, themes_drop: list) -> bool:
        """由 API 端点调用，写入主题确认结果，解除 pipeline 阻塞。"""
        from services.agent_task_store import AgentTaskStore, AgentStatus
        store = AgentTaskStore.get_instance()
        task = store.get(parent_id)
        if not task:
            return False
        try:
            result = json.loads(task.result_json or "{}")
        except Exception:
            result = {}
        if result.get("pipeline_stage") != "waiting_theme_confirm":
            return False
        result.update({
            "pipeline_stage": "theme_confirmed",
            "themes_keep": themes_keep,
            "themes_drop": themes_drop,
            "confirmed_at": datetime.utcnow().isoformat(),
        })
        store.update_status(parent_id, AgentStatus.RUNNING, result_json=json.dumps(result, ensure_ascii=False))
        logger.info(f"[AggPipeline:{parent_id[-6:]}] 主题确认：keep={len(themes_keep)}, drop={len(themes_drop)}")
        return True
