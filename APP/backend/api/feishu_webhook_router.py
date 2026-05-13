"""
飞书 Webhook + 互动管理 API 路由 (Spec Phase 2)

端点:
  POST /api/feishu/webhook                接收 OpenClaw 转发的用户回复
  POST /api/feishu/push-analysis          推送需求分析卡片
  POST /api/feishu/push-prd               推送 PRD 结果卡片
  POST /api/feishu/process-batch          批量处理 pending_notify 需求（cron 调用）
  POST /api/feishu/auto-confirm-timeouts  自动确认超时会话 + 轮询PRD完成（cron 调用）
  GET  /api/feishu/sessions               查询会话列表
  GET  /api/feishu/sessions/{id}          查询会话详情
"""

import logging
import os
import requests as http_requests

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from services.feishu_interaction_service import get_interaction_service, compute_value_score
from requirements_pool_service import get_req_pool_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feishu", tags=["feishu"])

# 仅在指定的 cron 主机（Mini）上执行定时任务端点；QCL 纯应用端不设此变量
_IS_CRON_HOST = os.environ.get("CRON_HOST", "").lower() in ("1", "true")


# ─── 请求模型 ──────────────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    """OpenClaw 转发的用户消息"""
    session_id: Optional[str] = None   # 可选：不传时按 user_id 自动查找最近活跃会话
    user_id: Optional[str] = "unknown"
    message: str


class PushAnalysisRequest(BaseModel):
    user_id: str
    req_id: str
    req_title: str
    summary: str
    value_score: Optional[float] = None
    suggestion: str
    references: Optional[str] = None


class PushPrdRequest(BaseModel):
    session_id: str
    prd_path: str
    prd_size_kb: float = 0.0


class PushRevisedRequest(BaseModel):
    session_id: str
    summary: str
    value_score: Optional[float] = None
    suggestion: str
    references: Optional[str] = None


# ─── 端点 ──────────────────────────────────────────────────────────────────────

@router.post("/webhook")
async def receive_webhook(payload: WebhookPayload):
    """
    接收 OpenClaw 转发的用户回复，驱动会话状态机。

    两种调用方式：
    1. 指定会话：{"session_id": "xxx", "user_id": "qiangxiao", "message": "方向OK"}
    2. 自动查找：{"user_id": "qiangxiao", "message": "方向OK"}  ← 自动匹配该用户最近活跃会话
    """
    import re as _re
    svc = get_interaction_service()

    # JobMaster 待决策回复优先路由（不依赖是否有需求会话，避免回复被吞）
    if not payload.session_id and _re.match(
        r"(?:执行|同意)\s*#?[A-Z0-9]{8}", payload.message.strip(), _re.IGNORECASE
    ):
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
            from jobmaster_agent import JobMasterAgent
            result = JobMasterAgent().process_user_reply(payload.message)
            if result.get("matched"):
                return {"action": "jobmaster_decision", **result}
            # 没匹配到决策 ID → fall through 继续走需求会话路由
        except Exception as _e:
            logger.warning(f"[webhook] JobMaster decision routing failed: {_e}")

    session_id = payload.session_id
    if not session_id:
        # 按 user_id 查找最近 pending_review 会话
        sessions = svc.list_sessions(user_id=payload.user_id, status="pending_review")
        if not sessions:
            return {"action": "no_active_session", "user_id": payload.user_id}
        # 取最新创建的
        sessions.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        session_id = sessions[0]["session_id"]
        logger.info(f"[webhook] user_id={payload.user_id} 自动匹配会话 {session_id}")

    result = svc.handle_user_reply(session_id, payload.message)
    return result


@router.post("/push-analysis")
async def push_analysis(req: PushAnalysisRequest):
    """推送需求分析卡片到飞书（由需求池分析或调度任务调用）"""
    svc = get_interaction_service()
    analysis = {
        "summary": req.summary,
        "value_score": req.value_score,
        "suggestion": req.suggestion,
        "references": req.references,
    }
    session_id = svc.push_analysis_card(req.user_id, req.req_id, req.req_title, analysis)
    return {"session_id": session_id, "req_id": req.req_id}


@router.post("/push-revised")
async def push_revised(req: PushRevisedRequest):
    """推送修订版分析卡片（多轮循环）"""
    svc = get_interaction_service()
    revised_analysis = {
        "summary": req.summary,
        "value_score": req.value_score,
        "suggestion": req.suggestion,
        "references": req.references,
    }
    ok = svc.push_revised_card(req.session_id, revised_analysis)
    if not ok:
        raise HTTPException(status_code=404, detail=f"会话 {req.session_id} 不存在")
    return {"pushed": True, "session_id": req.session_id}


@router.post("/push-prd")
async def push_prd_result(req: PushPrdRequest):
    """PRD 生成完成后推送结果卡片"""
    svc = get_interaction_service()
    ok = svc.push_prd_result(req.session_id, req.prd_path, req.prd_size_kb)
    if not ok:
        raise HTTPException(status_code=404, detail=f"会话 {req.session_id} 不存在")
    return {"pushed": True, "session_id": req.session_id}


@router.post("/process-batch")
async def process_batch_notifications():
    """
    批量处理 pending_notify 需求：计算价值评分，推送分析卡片，标记已通知。
    由 Mac Mini OpenClaw cron 每 2 分钟调用。仅在 CRON_HOST=true 的实例执行。
    """
    if not _IS_CRON_HOST:
        return {"processed": 0, "results": [], "skipped": "not_cron_host"}
    svc = get_interaction_service()
    rps = get_req_pool_service()
    if rps is None:
        return {"processed": 0, "results": [], "error": "requirements_pool_service not initialized"}

    try:
        reqs = (rps.get_pending_notify() or [])[:5]
    except Exception as e:
        logger.error(f"[process-batch] 获取 pending_notify 失败: {e}")
        return {"processed": 0, "results": [], "error": str(e)}

    results = []
    for r in reqs:
        req_id = r["req_id"]
        # 跳过已有活跃会话的需求
        existing = svc.get_session_by_req(req_id)
        if existing and existing.get("status") not in ("prd_done", "deferred"):
            logger.info(f"[process-batch] 跳过已有活跃会话: {req_id} status={existing.get('status')}")
            continue

        ai = r.get("ai_analysis", {}) or {}
        individual = (ai.get("individual", {}) or {})
        batch_ctx = (ai.get("batch_context", {}) or {})

        # 查找相似需求（反映需求普遍性，用于评分加分）
        similar_reqs = []
        try:
            from search_chroma import get_vector_store
            vs = get_vector_store()
            if vs:
                _query = f"{r.get('title', '')} {(r.get('description', '') or '')[:200]}"
                similar_reqs = vs.search_similar_requirements(query=_query, top_k=10)
                # 手动过滤：排除自身 + 相似度阈值 0.65
                similar_reqs = [sr for sr in similar_reqs if sr.get('req_id') != req_id and sr.get('score', 0) >= 0.65]
        except Exception:
            pass

        value_score = compute_value_score(ai, similar_count=len(similar_reqs))
        summary = individual.get("core_problem", "") or individual.get("summary", "")
        summary = summary[:200] if summary else ""
        suggestion = (
            individual.get("mvp_suggestion", "")
            or individual.get("detailed_solution", "")
            or ""
        )
        suggestion = suggestion[:300]
        refs = ", ".join(r.get("source_issues", []) or [])
        acceptance_criteria = individual.get("acceptance_criteria") or []

        analysis = {
            "value_score": value_score,
            "summary": summary,
            "suggestion": suggestion,
            "references": refs,
            "acceptance_criteria": acceptance_criteria,
            "similar_requirement_ids": [sr.get('req_id', '') for sr in similar_reqs[:5]],
            "similar_requirement_count": len(similar_reqs),
        }

        try:
            session_id = svc.push_analysis_card(
                user_id="qiangxiao",
                req_id=req_id,
                req_title=r.get("title", req_id),
                analysis=analysis,
            )
        except Exception as e:
            logger.error(f"[process-batch] push_analysis_card 失败 {req_id}: {e}")
            results.append({"req_id": req_id, "error": str(e)})
            continue

        # 标记已通知
        try:
            rps.mark_feishu_notified(req_id)
        except Exception as e:
            logger.warning(f"[process-batch] mark_notified 失败 {req_id}: {e}")

        results.append({"req_id": req_id, "session_id": session_id, "value_score": value_score})
        logger.info(f"[process-batch] 推送完成: {req_id} score={value_score} session={session_id}")

    return {"processed": len(results), "results": results}


@router.post("/auto-confirm-timeouts")
async def auto_confirm_timeouts(timeout_seconds: int = 60):
    """
    自动确认超时的 pending_review 会话（默认60秒），触发PRD生成。
    同时轮询 prd_generating 会话检查完成情况。
    由 Mac Mini OpenClaw cron 每 1 分钟调用。仅在 CRON_HOST=true 的实例执行。
    """
    if not _IS_CRON_HOST:
        return {"auto_confirmed": [], "prd_completed": [], "skipped": "not_cron_host"}
    svc = get_interaction_service()
    confirmed = svc.auto_confirm_timed_out_sessions(timeout_seconds)
    completed = svc.poll_prd_tasks()
    return {"auto_confirmed": confirmed, "prd_completed": completed}


@router.get("/sessions")
async def list_sessions(user_id: Optional[str] = None, status: Optional[str] = None):
    """查询飞书会话列表"""
    svc = get_interaction_service()
    sessions = svc.list_sessions(user_id=user_id, status=status)
    return {"total": len(sessions), "sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """查询会话详情"""
    svc = get_interaction_service()
    session = svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    return session
