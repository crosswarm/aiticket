"""
PM 协作任务看板 API 路由
"""

import os
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import APIRouter, Body, Header, HTTPException, Query, Request

_IS_DEMO = os.getenv("IS_DEMO_INSTANCE", "").lower() in ("1", "true")
from pydantic import BaseModel

from models.pm_models import (
    PMDemand,
    PredefineData,
    PredefineCreateRequest,
    PMBoardResponse,
    PMBoardStats,
    AutoProcessStatus,
    SyncResult,
    DemandStatus,
)
from services.pm_collaboration_service import get_pm_service
from services.pm_scheduler import get_pm_scheduler
from services.pm_notifier import get_pm_notifier
from services.pm_wallet_service import PMNotBoundError

router = APIRouter(prefix="/api/pm", tags=["PM协作任务"])


# ============== 看板数据 API ==============

@router.get("/board", response_model=PMBoardResponse)
def get_pm_board_data(
    status: Optional[str] = Query(None, description="状态筛选: WAIT_ANALYSIS/COO_ACCEPT/COO_HANG"),
    overdue_only: bool = Query(False, description="仅显示已超时"),
    limit: int = Query(100, description="返回数量限制"),
):
    """
    获取PM看板数据
    """
    service = get_pm_service()

    # 获取统计数据
    stats = service.get_stats()

    # 获取需求列表
    status_enum = None
    if status:
        try:
            status_enum = DemandStatus(status)
        except ValueError:
            pass

    demands = service.get_cached_demands(
        status=status_enum,
        overdue_only=overdue_only,
    )[:limit]

    return PMBoardResponse(
        status="success",
        data={
            "demands": [d.model_dump() for d in demands],
            "count": len(demands),
        },
        stats=stats,
    )


@router.get("/stats")
def get_pm_stats():
    """
    获取PM看板统计数据
    """
    service = get_pm_service()
    stats = service.get_stats()
    return {"status": "success", "data": stats.model_dump()}


@router.get("/demands/{demand_id}")
def get_demand_detail(demand_id: str):
    """
    获取需求详情
    """
    service = get_pm_service()
    demands = service.get_cached_demands()

    for demand in demands:
        if demand.aid == demand_id or demand.code == demand_id:
            return {"status": "success", "data": demand.model_dump()}

    raise HTTPException(status_code=404, detail="需求不存在")


# ============== 预定义协作 API ==============

@router.get("/predefines")
def list_predefines(
    active_only: bool = Query(True, description="仅显示有效的预定义"),
):
    """
    列出所有预定义协作
    """
    service = get_pm_service()
    predefines = service.predefine_manager.list_all(active_only=active_only)

    return {
        "status": "success",
        "data": [p.model_dump() for p in predefines],
        "count": len(predefines),
    }


@router.post("/predefines")
def create_predefine(data: PredefineCreateRequest):
    """
    创建预定义协作
    """
    service = get_pm_service()

    # 计算期望解决时间
    expected_resolve_time = None
    if data.expected_resolve_days:
        expected_resolve_time = datetime.now() + timedelta(days=data.expected_resolve_days)

    predefine_data = {
        "proposer_name": data.proposer_name,
        "proposer_domain": data.proposer_domain,
        "expected_resolve_time": expected_resolve_time,
        "keywords": data.keywords,
        "auto_accept": data.auto_accept,
        "description": data.description,
        "created_by": "system",  # TODO: 从当前用户获取
    }

    predefine = service.predefine_manager.create(predefine_data)

    # 发送通知
    notifier = get_pm_notifier()
    notifier.notify_new_predefine(predefine)

    return {
        "status": "success",
        "data": predefine.model_dump(),
        "message": "预定义创建成功",
    }


@router.delete("/predefines/{predefine_id}")
def delete_predefine(predefine_id: str):
    """
    删除预定义协作
    """
    service = get_pm_service()
    success = service.predefine_manager.delete(predefine_id)

    if not success:
        raise HTTPException(status_code=404, detail="预定义不存在")

    return {"status": "success", "message": "预定义删除成功"}


# ============== 自动处理 API ==============

@router.get("/auto-process/status")
def get_auto_process_status():
    """
    获取自动处理状态
    """
    scheduler = get_pm_scheduler()
    status = scheduler.get_status()

    return {
        "status": "success",
        "data": {
            "enabled": status["running"],
            "running": status["running"],
            "last_run_at": status["last_process_at"],
            "processed_count": status["total_processed"],
            "today_processed": status["today_processed"],
            "sync_interval": status["sync_interval"],
            "process_interval": status["process_interval"],
        },
    }


@router.post("/auto-process/toggle")
def toggle_auto_process(enabled: bool):
    """
    切换自动处理开关
    """
    scheduler = get_pm_scheduler()

    if enabled and not scheduler.is_running():
        scheduler.start()
    elif not enabled and scheduler.is_running():
        scheduler.stop()

    return {
        "status": "success",
        "data": {"enabled": enabled},
        "message": f"自动处理已{'启用' if enabled else '禁用'}",
    }


# ============== 手动触发 API ==============

@router.post("/sync")
def trigger_sync():
    """
    手动触发数据同步
    """
    if _IS_DEMO:
        return {"status": "demo_blocked", "message": "演示模式：PM 同步已屏蔽"}
    scheduler = get_pm_scheduler()
    result = scheduler.trigger_sync()

    if result["success"]:
        return {
            "status": "success",
            "data": result,
            "message": f"同步完成: 总数 {result.get('total', 0)}, 新增 {result.get('new', 0)}",
        }
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "同步失败"))


@router.post("/process")
def trigger_process():
    """
    手动触发自动处理
    """
    scheduler = get_pm_scheduler()
    result = scheduler.trigger_process()

    if result["success"]:
        return {
            "status": "success",
            "data": result,
            "message": f"处理完成: 共 {result.get('processed_count', 0)} 个需求",
        }
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "处理失败"))


@router.post("/check-overdue")
def trigger_overdue_check():
    """
    手动触发超时检查
    """
    service = get_pm_service()
    notifier = get_pm_notifier()

    overdue = service.overdue_monitor.check_overdue_demands()

    if overdue:
        notifier.notify_overdue_demands(overdue)

    return {
        "status": "success",
        "data": {
            "overdue_count": len(overdue),
            "demands": [d.model_dump() for d in overdue],
        },
        "message": f"发现 {len(overdue)} 个超时需求",
    }


# ============== 配置 API ==============

@router.get("/config")
def get_pm_config():
    """
    获取PM系统配置
    """
    service = get_pm_service()
    config = service.config

    # 隐藏敏感信息
    safe_config = {
        "base_url": config.get("base_url"),
        "line_id": config.get("line_id"),
        "sync_interval": config.get("sync", {}).get("interval_minutes"),
        "process_interval": config.get("auto_process", {}).get("interval_minutes"),
        "overdue_threshold": config.get("overdue", {}).get("threshold_days"),
    }

    return {"status": "success", "data": safe_config}


# ============== 前端页面路由 ==============

# ===========================================================================
# 模块化 API — PMModuleService（feat-原始需求，2026-04-14）
# 路径: /api/pm/modules/{module_key}/...
# 不写死 entityType，通过 module_key 查 pm_config.yaml
# ===========================================================================

from services.pm_module_service import get_pm_module_service
from models.pm_models import TriageActionRequest


def _resolve_pm_user(request: Request) -> str | None:
    """X-PM-User 必须与已认证用户名匹配，防止客户端伪造。"""
    current_user = getattr(request.state, "current_user", None)
    if not current_user:
        raise HTTPException(status_code=401, detail="未登录")
    header_user = request.headers.get("X-PM-User", "").strip()
    if header_user and header_user != current_user["username"]:
        raise HTTPException(status_code=403, detail="X-PM-User 与登录用户不匹配")
    return header_user or current_user["username"]


def _svc(module_key: str, request: Request):
    """获取 PMModuleService 并注入当前用户（用于钱包路由）。"""
    svc = get_pm_module_service(module_key)
    svc.current_pm_user = _resolve_pm_user(request)
    return svc


@router.get("/modules")
def list_modules():
    """列出所有已配置的 PM 模块"""
    from pathlib import Path
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "pm_config.yaml"
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f).get("pm_system", {})
        modules = cfg.get("modules", {})
        return {
            "status": "success",
            "modules": [
                {"key": k, "label": v.get("label", k), "entity_type": v.get("entity_type")}
                for k, v in modules.items()
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modules/{module_key}/info")
def get_module_info(module_key: str):
    """获取模块配置信息（含状态枚举、允许操作等）"""
    try:
        svc = get_pm_module_service(module_key)
        return {"status": "success", "data": svc.get_module_info()}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/modules/{module_key}/token")
def check_module_token(module_key: str):
    """验证模块 token 有效性"""
    try:
        svc = get_pm_module_service(module_key)
        result = svc.check_token_valid()
        return {"status": "success" if result["valid"] else "error", "data": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class ModuleDemandQuery(BaseModel):
    page: int = 1
    page_size: int = 30
    status_filter: Optional[List[str]] = None
    order_by: str = "ctime"
    is_asc: bool = False
    assignee_id: Optional[str] = None
    current_user_only: bool = True   # 默认按当前配置用户过滤（与 PM 页面默认行为一致）


@router.post("/modules/{module_key}/demands")
def query_module_demands(module_key: str, query: ModuleDemandQuery, request: Request = None):
    """
    分页查询模块需求列表。
    - current_user_only=true（默认）: 只返回当前用户（config.default_analyst）负责的需求
    - current_user_only=false: 返回整个产线的需求
    - status_filter: 状态过滤，如 ["WAIT_ANALYSIS", "ASSIGNING"]
    """
    if _IS_DEMO and module_key == "collaboration_demand":
        return {"status": "success", "data": {"success": True, "records": [], "total": 0,
                                               "page": query.page, "page_size": query.page_size,
                                               "message": "演示模式：协作需求暂无基线数据"}}
    try:
        svc = _svc(module_key, request)
        result = svc.fetch_demands(
            page=query.page,
            page_size=query.page_size,
            status_filter=query.status_filter,
            order_by=query.order_by,
            is_asc=query.is_asc,
            assignee_id=query.assignee_id,
            current_user_only=query.current_user_only,
        )
        if result["success"]:
            return {"status": "success", "data": result}
        return {"status": "error", "message": result.get("message")}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/modules/{module_key}/demands/{aid}")
def get_module_demand_detail(module_key: str, aid: str, request: Request):
    """获取单条需求详情"""
    try:
        svc = _svc(module_key, request)
        result = svc.get_demand_detail(aid)
        if result["success"]:
            return {"status": "success", "data": result["record"]}
        raise HTTPException(status_code=404, detail=result.get("message"))
    except PMNotBoundError as e:
        raise HTTPException(status_code=401, detail={"code": "pm_not_bound", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/modules/{module_key}/demands/{aid}/history")
def get_module_demand_history(module_key: str, aid: str, request: Request):
    """获取需求流程记录（状态变更、评论历史）"""
    try:
        svc = _svc(module_key, request)
        result = svc.get_process_history(aid)
        return {"status": "success", "records": result.get("records", []),
                "message": result.get("message", "")}
    except PMNotBoundError as e:
        raise HTTPException(status_code=401, detail={"code": "pm_not_bound", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/modules/{module_key}/demands/{aid}/operations")
def get_available_operations(module_key: str, aid: str, request: Request):
    """获取需求当前可用的操作列表（动态，基于需求当前状态）"""
    try:
        svc = _svc(module_key, request)
        result = svc.get_available_operations(aid)
        if result.get("error_code"):
            return {"status": "error", "error_code": result["error_code"], "message": result["message"], "operations": []}
        return {"status": "success", "operations": result.get("operations", [])}
    except PMNotBoundError as e:
        raise HTTPException(status_code=401, detail={"code": "pm_not_bound", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/modules/{module_key}/demands/{aid}/process-rules")
def get_process_rules(module_key: str, aid: str, action: str, request: Request, current_status: str = "WAIT_ANALYSIS"):
    """
    获取 PM workflow 操作的表单规则（哪些字段必填/可编辑）。
    前端用此接口动态渲染操作弹窗表单。
    """
    try:
        svc = _svc(module_key, request)
        result = svc.get_process_rules(action=action, current_status=current_status)
        if result.get("error_code"):
            return {"status": "error", "error_code": result["error_code"], "message": result["message"], "fields": []}
        return {"status": "success" if result["success"] else "error", "fields": result.get("fields", []), "message": result.get("message", "")}
    except PMNotBoundError as e:
        raise HTTPException(status_code=401, detail={"code": "pm_not_bound", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/modules/{module_key}/demands/{aid}/action")
def execute_module_action(module_key: str, aid: str, request: TriageActionRequest, req: Request = None):
    """
    执行 PM 原生操作（accept/reject/hang/comment/...）。
    走 workflow 引擎：POST /rest/v1/workflow/processConvert。
    request.pm_action 指定操作，request.pm_payload.fieldData 包含表单字段值。
    """
    try:
        svc = _svc(module_key, req)
        action = request.pm_action or request.action
        # 从 payload 中提取 currentStatus（前端应传入需求当前状态）
        current_status = (request.pm_payload or {}).get("currentStatus", "WAIT_ANALYSIS")
        result = svc.execute_action(aid=aid, action=action, payload=request.pm_payload, current_status=current_status)
        status = "success" if result["success"] else "error"
        resp = {"status": status, "message": result.get("message"), "data": result.get("data")}
        if result.get("error_code"):
            resp["error_code"] = result["error_code"]
        return resp
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ===========================================================================
# 分诊 API — PMTriageService（Phase 2）
# ===========================================================================

from services.pm_triage_service import get_pm_triage_service


@router.get("/modules/{module_key}/triage/summary")
def get_triage_summary(module_key: str,
                       demand_ids: Optional[str] = Query(None, description="逗号分隔的 aid 列表，用于只统计指定需求的缓存（前端传入已过滤的看板 aid）")):
    """获取分诊看板总结（进入 Tab 时调用）。
    支持 demand_ids 过滤：前端将当前可见的 aid 列表传入，只统计这些需求的已分析缓存，无需外部 PM API 调用。
    """
    try:
        triage = get_pm_triage_service()
        allowed_aids = None
        if demand_ids:
            allowed_aids = {a.strip() for a in demand_ids.split(",") if a.strip()}
        summary = triage.summarize_board(allowed_aids=allowed_aids)
        return {"status": "success", "data": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/modules/{module_key}/triage/analyze/{aid}")
def triage_analyze_one(module_key: str, aid: str, request: Request, force: bool = False):
    """分析单条需求（force=true 忽略缓存重新分析）"""
    try:
        svc = _svc(module_key, request)
        detail = svc.get_demand_detail(aid)
        if not detail["success"]:
            raise HTTPException(status_code=404, detail=f"需求 {aid} 不存在")
        triage = get_pm_triage_service()
        result = triage.analyze_demand(detail["record"], force=force)
        if result["success"]:
            return {"status": "success", "data": result["analysis"], "from_cache": result.get("from_cache")}
        return {"status": "error", "message": result.get("message")}
    except HTTPException:
        raise
    except PMNotBoundError as e:
        raise HTTPException(status_code=401, detail={"code": "pm_not_bound", "message": str(e)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class BatchAnalyzeRequest(BaseModel):
    status_filter: Optional[List[str]] = None
    assignee_id: Optional[str] = None
    current_user_only: bool = False      # 默认 False，与看板"全部"显示保持一致
    page_size: int = 20
    force: bool = False
    concurrency: int = 3
    product_ids: Optional[List[str]] = None  # 应用/服务过滤（productId 编码列表）


@router.post("/modules/{module_key}/triage/batch-analyze")
def triage_batch_analyze(module_key: str, req: BatchAnalyzeRequest, request: Request):
    """批量分析需求（异步任务）——自动翻页，分析全部待分诊条目"""
    import threading
    try:
        svc = _svc(module_key, request)
        status_filter = req.status_filter or svc.module.get("triage_target_statuses",
                                                             ["WAIT_ANALYSIS", "ASSIGNING"])
        fetch_fields = svc.module.get("fetch_fields", []) + ["description"]
        batch_size = min(req.page_size, 50)   # 单页最多 50，避免 PM 接口超时

        # 构建应用/服务过滤条件
        extra_conditions = []
        if req.product_ids:
            extra_conditions.append({
                "fieldCode": "productId",
                "operation": "in",
                "valueType": "STRING",
                "editType": "LIST",
                "values": req.product_ids,
            })

        # 翻页拉取全部待分析条目
        all_demands: list = []
        page = 1
        while True:
            r = svc.fetch_demands(
                page=page, page_size=batch_size,
                status_filter=status_filter,
                current_user_only=req.current_user_only,
                assignee_id=req.assignee_id,
                fetch_fields=fetch_fields,
                extra_conditions=extra_conditions if extra_conditions else None,
            )
            if not r["success"]:
                if page == 1:
                    return {"status": "error", "message": r.get("message")}
                break   # 后续页失败时，用已拿到的数据继续
            records = r["records"]
            all_demands.extend(records)
            total = r.get("total", 0)
            # 已拿到全部 或 本页不足一整页（已是最后一页）
            if len(all_demands) >= total or len(records) < batch_size:
                break
            page += 1

        if not all_demands:
            return {"status": "ok", "queued": 0, "message": "没有待分析的需求（全部已分析或无数据）"}

        task_id = f"batch-triage-{int(datetime.now().timestamp())}"

        # 在后台线程执行（不阻塞请求）
        def run_batch():
            triage = get_pm_triage_service()
            triage.batch_analyze(all_demands, concurrency=req.concurrency, force=req.force)

        threading.Thread(target=run_batch, daemon=True).start()

        return {
            "status": "accepted",
            "task_id": task_id,
            "queued": len(all_demands),
            "total_pages": page,
            "message": f"已启动批量分析，共 {len(all_demands)} 条需求（后台执行，约 {len(all_demands) * 5} 秒）",
        }
    except PMNotBoundError as e:
        raise HTTPException(status_code=401, detail={"code": "pm_not_bound", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modules/{module_key}/triage/board")
def get_triage_board(module_key: str, page: int = 1, page_size: int = 30,
                     current_user_only: bool = True, request: Request = None):
    """
    返回分诊看板数据（按分诊决策分桶）。
    结构: {pending: [], auto_reject: [], auto_alternative: [], manual: []}
    """
    try:
        # demo 沙箱：PM 网络不可用，直接从本地 pm_triage/ 缓存构建看板
        if _IS_DEMO:
            triage = get_pm_triage_service()
            board: Dict[str, List] = {"pending": [], "auto_reject": [], "auto_alternative": [], "manual": []}
            from services.pm_triage_service import _TRIAGE_CACHE_DIR
            import json as _json
            for p in sorted(_TRIAGE_CACHE_DIR.glob("*.json")):
                try:
                    with open(p) as f:
                        cached = _json.load(f)
                except Exception:
                    continue
                decision = cached.get("triage_decision") or "pending"
                if decision not in board:
                    decision = "pending"
                board[decision].append({"aid": p.stem, "ai_analysis": cached, "triage_decision": decision,
                                        "title": cached.get("title", ""), "summary": cached.get("summary", "")})
            return {"status": "success", "total": sum(len(v) for v in board.values()),
                    "board": board, "counts": {k: len(v) for k, v in board.items()}}

        svc = _svc(module_key, request) if request else get_pm_module_service(module_key)
        triage = get_pm_triage_service()
        status_filter = svc.module.get("triage_target_statuses", ["WAIT_ANALYSIS", "ASSIGNING"])
        r = svc.fetch_demands(page=page, page_size=page_size,
                              status_filter=status_filter,
                              current_user_only=current_user_only)
        if not r["success"]:
            return {"status": "error", "message": r.get("message")}

        board: Dict[str, List] = {"pending": [], "auto_reject": [], "auto_alternative": [], "manual": []}
        for rec in r["records"]:
            cached = triage._load_cached_public(rec.get("aid", ""))
            if cached:
                decision = triage.decide(rec, cached)
                entry = {**rec, "ai_analysis": cached, "triage_decision": decision}
                board[decision].append(entry)
            else:
                board["pending"].append({**rec, "triage_decision": "pending"})

        return {
            "status": "success",
            "total": r["total"],
            "board": board,
            "counts": {k: len(v) for k, v in board.items()},
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pm-board-page")
def pm_board_page():
    """
    获取看板页面HTML
    """
    from fastapi.responses import FileResponse
    from pathlib import Path

    page_path = Path(__file__).parent.parent.parent / "frontend" / "pm_board.html"
    if page_path.exists():
        return FileResponse(page_path)
    else:
        raise HTTPException(status_code=404, detail="页面不存在")


# ============================================================
# 智能总结导出 + 批量暂缓低分
# ============================================================

# 批量暂缓任务进度表（内存存储，重启后丢失，符合预期）
_batch_hang_tasks: Dict[str, Dict] = {}


def _make_hang_reply(analysis: dict) -> str:
    """根据分析缓存生成 ≤20 字的暂缓回复。"""
    alt = (analysis.get("alternative_solution") or "").strip()
    reason = (analysis.get("triage_reason") or "").strip()
    if alt:
        short_alt = alt[:8].rstrip("，。,.")
        return f"已暂缓，可参考：{short_alt}"[:20]
    if reason:
        short_r = reason[:8].rstrip("，。,.")
        return f"感谢反馈，{short_r}暂缓"[:20]
    return "感谢需求，暂缓处理"


def _run_batch_hang(task_id: str, aids: List[str], module_key: str) -> None:
    """后台线程：逐条执行暂缓 + 评论操作，更新进度表。"""
    import threading
    from services.pm_triage_service import _TRIAGE_CACHE_DIR
    svc = get_pm_module_service(module_key)
    triage = get_pm_triage_service()
    progress = _batch_hang_tasks[task_id]

    for aid in aids:
        try:
            analysis = triage._load_cached_public(aid) or {}
            # 获取需求当前状态（用于 workflow 操作）
            detail = svc.get_demand_detail(aid)
            current_status = "WAIT_ANALYSIS"
            if detail.get("success") and detail.get("record"):
                current_status = detail["record"].get("status", "WAIT_ANALYSIS")
            # 执行暂缓
            hang_r = svc.execute_action(aid, "hang", payload={}, current_status=current_status)
            ok = hang_r.get("success", False)
            if ok:
                # 补充暂缓回复评论
                reply = _make_hang_reply(analysis)
                svc.execute_action(aid, "comment",
                                   payload={"comment": reply},
                                   current_status="WAIT_PROCESS")
            else:
                progress["errors"].append({
                    "aid": aid,
                    "code": analysis.get("code", aid),
                    "error": hang_r.get("message", "未知错误"),
                })
            progress["current_item"] = {
                "aid": aid,
                "code": analysis.get("code", aid),
                "ok": ok,
            }
        except Exception as e:
            progress["errors"].append({"aid": aid, "error": str(e)})
        finally:
            progress["done"] = progress.get("done", 0) + 1

    progress["completed"] = True
    progress["status"] = "done"


@router.post("/modules/{module_key}/triage/summary/export-markdown")
def export_triage_summary_markdown(
    module_key: str,
    demand_ids: Optional[str] = Query(None, description="逗号分隔的 aid，用于过滤（同 summary 端点）"),
    label: Optional[str] = Query(None, description="过滤标签，如'工作流'，写入文件名和摘要"),
):
    """将当前智能总结导出为 Markdown 文件，保存到 conclusion/exports/。"""
    from pathlib import Path
    try:
        triage = get_pm_triage_service()
        allowed_aids = None
        if demand_ids:
            allowed_aids = {a.strip() for a in demand_ids.split(",") if a.strip()}
        summary = triage.summarize_board(allowed_aids=allowed_aids)
        md_text = triage.generate_summary_markdown(summary, label=label or "")
        # 保存路径：conclusion/exports/智能需求分析总结-{date}-{label}.md
        exports_dir = Path(__file__).parent.parent.parent.parent / "conclusion" / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d-%H%M")
        safe_label = (label or "全部").replace("/", "-").replace(" ", "")
        filename = f"智能需求分析总结-{date_str}-{safe_label}.md"
        filepath = exports_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_text)
        return {
            "status": "success",
            "filename": filename,
            "path": str(filepath),
            "saved": True,
            "total_analyzed": summary.get("total_analyzed", 0),
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/modules/{module_key}/triage/batch-hang-low-score")
def start_batch_hang_low_score(
    module_key: str,
    score_threshold: int = Query(60, description="分值阈值，低于此分值的需求将被暂缓"),
    demand_ids: Optional[List[str]] = Body(None, description="可选：只处理这些 aid（默认全部低分）"),
):
    """批量暂缓已分析中 value_score < threshold 的需求（后台执行，返回 task_id 供进度查询）。"""
    import threading
    from services.pm_triage_service import _TRIAGE_CACHE_DIR
    try:
        triage = get_pm_triage_service()
        # 扫描缓存，筛出低分 aids
        low_score_aids = []
        for p in _TRIAGE_CACHE_DIR.glob("*.json"):
            try:
                import json as _json
                with open(p) as f:
                    a = _json.load(f)
                score = triage._safe_int(a.get("value_score"), 50)
                if score < score_threshold:
                    low_score_aids.append(p.stem)
            except Exception:
                continue
        # 若前端传了 demand_ids，取交集
        if demand_ids:
            aids_set = set(demand_ids)
            low_score_aids = [a for a in low_score_aids if a in aids_set]
        if not low_score_aids:
            return {"status": "ok", "total": 0, "message": f"没有 value_score < {score_threshold} 的已分析需求"}
        task_id = f"batch-hang-{int(datetime.now().timestamp())}"
        _batch_hang_tasks[task_id] = {
            "done": 0,
            "total": len(low_score_aids),
            "completed": False,
            "status": "running",
            "errors": [],
            "current_item": None,
        }
        threading.Thread(
            target=_run_batch_hang,
            args=(task_id, low_score_aids, module_key),
            daemon=True,
        ).start()
        return {
            "status": "accepted",
            "task_id": task_id,
            "total": len(low_score_aids),
            "message": f"已启动批量暂缓，共 {len(low_score_aids)} 条（score < {score_threshold}），后台执行中",
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modules/{module_key}/triage/batch-hang-progress/{task_id}")
def get_batch_hang_progress(module_key: str, task_id: str):
    """查询批量暂缓任务进度。"""
    task = _batch_hang_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在（已过期或未启动）")
    return task


# ============== PM Session 钱包 API ==============

from services.pm_wallet_service import (
    save_user_token, get_binding_status, list_bindings
)


class PMCookieUpload(BaseModel):
    yht_access_token: str
    tenant_info: str = "0000"
    extra_cookies: Dict[str, str] = {}
    proxy_endpoint: str = ""  # 用户代理机地址，留空则自动从客户端 IP 生成


@router.get("/session/me/status")
def get_pm_session_status(x_pm_user: Optional[str] = Header(None)):
    """查询当前用户的 PM 会话绑定状态。"""
    if not x_pm_user:
        return {"bound": False, "reason": "no X-PM-User header"}
    return get_binding_status(x_pm_user)


def _get_client_ip(request: Request) -> str:
    """获取真实客户端 IP（优先 X-Forwarded-For / X-Real-IP）。"""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    real_ip = request.headers.get('X-Real-IP', '')
    if real_ip:
        return real_ip
    return request.client.host if request.client else ''


@router.post("/session/me/upload")
def upload_pm_session(
    payload: PMCookieUpload,
    request: Request,
    x_pm_user: Optional[str] = Header(None),
):
    """上传当前用户的 PM cookies 到钱包（本地 + 同步到 Mini jira_proxy）。
    自动检测用户 IP 作为 proxy_endpoint，用户的 PM 请求将通过其自身机器路由。
    """
    if not x_pm_user:
        raise HTTPException(status_code=400, detail="缺少 X-PM-User 请求头（需在前端登录后自动注入）")
    if not payload.yht_access_token:
        raise HTTPException(status_code=400, detail="yht_access_token 不能为空")
    # 自动检测客户端 IP → 默认 proxy_endpoint
    data = payload.model_dump()
    if not data.get('proxy_endpoint'):
        client_ip = _get_client_ip(request)
        if client_ip and client_ip not in ('127.0.0.1', '::1'):
            data['proxy_endpoint'] = f"http://{client_ip}:3128"
    record = save_user_token(x_pm_user, data)
    # 同步到 Mini jira_proxy（钱包文件必须在 Mini 本地才能被 /pmf_forward 读取）
    import os, logging as _log
    pm_base = os.environ.get("PM_BASE_URL", "").rstrip("/")
    if pm_base and "pmf_forward" in pm_base:
        proxy_base = pm_base.rsplit("/pmf_forward", 1)[0]
        try:
            import requests as _req
            _req.post(f"{proxy_base}/pmf_wallet_save", json={
                "username": x_pm_user, "token_data": record
            }, timeout=5)
        except Exception as e:
            _log.getLogger(__name__).warning(f"[pm_wallet] sync to Mini failed: {e}")
    return {
        "status": "success",
        "message": f"PM session 已绑定到 {x_pm_user}",
        "expires_at": record["expires_at"],
        "proxy_endpoint": record.get("proxy_endpoint", ""),
    }


@router.get("/session/bindings")
def get_pm_session_bindings():
    """列出所有 PM 会话绑定（管理用）。"""
    return {"bindings": list_bindings()}


@router.get("/session/my-ip")
def get_my_ip(request: Request):
    """返回服务端检测到的客户端 IP，供前端展示预期 proxy_endpoint。"""
    ip = _get_client_ip(request)
    is_local = ip in ('127.0.0.1', '::1', '')
    return {
        "ip": ip,
        "proxy_endpoint": f"http://{ip}:3128" if not is_local else "",
        "is_local": is_local,
    }


@router.post("/session/proxy-test")
def test_proxy_connection(request: Request):
    """测试 mini 能否通过客户端机器的 CONNECT 代理到达 pm.example.com。"""
    import requests as _req, urllib3 as _u3
    _u3.disable_warnings()
    client_ip = _get_client_ip(request)
    if client_ip in ('127.0.0.1', '::1', ''):
        return {"ok": True, "message": "本机访问，无需外部代理"}
    proxy_ep = f"http://{client_ip}:3128"
    try:
        r = _req.head(
            "https://pm.example.com",
            proxies={"https": proxy_ep, "http": proxy_ep},
            timeout=8, verify=False,
        )
        return {"ok": True, "proxy": proxy_ep, "http_status": r.status_code}
    except Exception as e:
        err = str(e)
        # 407 means proxy exists but needs auth — still reachable
        if '407' in err:
            return {"ok": True, "proxy": proxy_ep, "message": "代理可达（需认证）"}
        return {"ok": False, "proxy": proxy_ep, "error": err[:300]}
