"""
JobMaster Router — Agent 生命周期管理 REST 接口（图书馆员模型）

端点：
  POST   /api/jobmaster/authorize            — Claude Code Task hook 授权检查（advisory）
  POST   /api/jobmaster/spawn                — 申请创建 agent（复用优先）
  GET    /api/jobmaster/catalog              — 调度中心体系视图（domain→category→agents）
  GET    /api/jobmaster/find_reusable        — 查找可复用 agent
  POST   /api/jobmaster/register_domain_root — 注册 domain root（仅 root token）
  PATCH  /api/jobmaster/agents/{id}/state   — 状态推进
  POST   /api/jobmaster/agents/{id}/heartbeat — 心跳上报
  POST   /api/jobmaster/agents/{id}/revoke  — 撤销 agent
  GET    /api/jobmaster/agents              — agent 列表
  GET    /api/jobmaster/agents/{id}         — 单 agent 详情
  GET    /api/jobmaster/events              — 事件日志
  GET    /api/jobmaster/mode                — 当前模式
  POST   /api/jobmaster/mode               — 切换模式（audit/enforce）
  POST   /api/jobmaster/audit/scan         — 手动触发巡查
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth_deps import require_admin_user
from services.jobmaster_lifecycle import JobMasterLifecycle

router = APIRouter(prefix="/api/jobmaster", tags=["jobmaster-lifecycle"],
                   dependencies=[Depends(require_admin_user)])


def _lc() -> JobMasterLifecycle:
    return JobMasterLifecycle.get_instance()


# ─── Schema ───────────────────────────────────────────────────────────────────

class AuthorizeRequest(BaseModel):
    session_id: str = ""
    description: str = ""
    subagent_type: str = ""

class SpawnRequest(BaseModel):
    agent_class: str
    parent_token: Optional[str] = None
    scope: Optional[list] = None
    context: Optional[Dict[str, Any]] = None
    domain: Optional[str] = None
    capabilities: Optional[list] = None
    purpose: Optional[str] = None

class DomainRootRequest(BaseModel):
    domain: str
    agent_id: str
    requester_token: str

class StateRequest(BaseModel):
    token: str
    new_state: str

class HeartbeatRequest(BaseModel):
    token: str

class RevokeRequest(BaseModel):
    reason: str = ""
    requester_token: Optional[str] = None

class ModeRequest(BaseModel):
    mode: str  # "audit" | "enforce"


# ─── 端点 ─────────────────────────────────────────────────────────────────────

@router.post("/authorize")
def authorize(req: AuthorizeRequest) -> Dict:
    """
    Claude Code PreToolUse hook 调用此接口决定是否放行 Task 工具。
    AUDIT 模式始终返回 allow=true；ENFORCE 模式按 spawn 权限校验。
    """
    result = _lc().authorize_claude_task(
        session_id=req.session_id,
        description=req.description,
        subagent_type=req.subagent_type,
    )
    return result


@router.post("/spawn")
def spawn(req: SpawnRequest) -> Dict:
    """申请创建 agent（复用优先）。reused=True 时建议复用已有 agent。"""
    result = _lc().spawn(
        agent_class_name=req.agent_class,
        parent_token=req.parent_token,
        scope=req.scope,
        context=req.context,
        domain=req.domain,
        capabilities=req.capabilities,
        purpose=req.purpose,
    )
    if not result["approved"] and _lc().mode == "enforce":
        raise HTTPException(status_code=403, detail=result["reason"])
    return result


@router.get("/catalog")
def catalog() -> Dict:
    """调度中心体系视图：{domain: {category: [agents...]}}"""
    return _lc().catalog()


@router.get("/find_reusable")
def find_reusable(
    domain: str = Query(...),
    capabilities: str = Query(""),
    threshold: float = Query(0.7, ge=0.0, le=1.0),
) -> Dict:
    """
    查找可复用 agent。capabilities 用逗号分隔。
    返回 {reused, agent_id, overlap_score, advisory} 或 {reused: false}。
    """
    caps = [c.strip() for c in capabilities.split(",") if c.strip()] if capabilities else []
    result = _lc().find_reusable(domain=domain, capabilities=caps, threshold=threshold)
    return result if result else {"reused": False, "domain": domain, "capabilities": caps}


@router.post("/register_domain_root")
def register_domain_root(req: DomainRootRequest) -> Dict:
    """指定 agent 为 domain root（仅 jobmaster_root token）。"""
    ok = _lc().register_domain_root(
        domain=req.domain, agent_id=req.agent_id, requester_token=req.requester_token
    )
    if not ok:
        raise HTTPException(status_code=403, detail="unauthorized or agent not found")
    return {"ok": True, "domain": req.domain, "agent_id": req.agent_id}


@router.patch("/agents/{agent_id}/state")
def transition_state(agent_id: str, req: StateRequest) -> Dict:
    ok = _lc().transition_state(agent_id, req.token, req.new_state)
    if not ok:
        raise HTTPException(status_code=400, detail="state transition failed or unauthorized")
    return {"ok": True, "agent_id": agent_id, "new_state": req.new_state}


@router.post("/agents/{agent_id}/heartbeat")
def heartbeat(agent_id: str, req: HeartbeatRequest) -> Dict:
    ok = _lc().heartbeat(agent_id, req.token)
    if not ok:
        raise HTTPException(status_code=401, detail="invalid token or agent_id")
    return {"ok": True}


@router.post("/agents/{agent_id}/revoke")
def revoke(agent_id: str, req: RevokeRequest) -> Dict:
    ok = _lc().revoke(agent_id, req.reason, req.requester_token)
    if not ok:
        raise HTTPException(status_code=403, detail="revoke failed or unauthorized")
    return {"ok": True, "agent_id": agent_id}


@router.get("/agents")
def list_agents(state: Optional[str] = Query(None)) -> Dict:
    agents = _lc().list_agents(state_filter=state)
    return {"agents": agents, "count": len(agents), "mode": _lc().mode}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str) -> Dict:
    agent = _lc().get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


@router.get("/events")
def list_events(
    limit: int = Query(100, le=500),
    agent_id: Optional[str] = Query(None),
) -> Dict:
    events = _lc().list_events(limit=limit, agent_id=agent_id)
    return {"events": events, "count": len(events)}


@router.get("/mode")
def get_mode() -> Dict:
    return {"mode": _lc().mode}


@router.post("/mode")
def set_mode(req: ModeRequest) -> Dict:
    ok = _lc().set_mode(req.mode)
    if not ok:
        raise HTTPException(status_code=400, detail="invalid mode, use 'audit' or 'enforce'")
    return {"ok": True, "mode": req.mode}


@router.post("/audit/scan")
def audit_scan() -> Dict:
    """手动触发巡查扫描（正常由 monitor cron 自动调用）。"""
    return _lc().audit_scan()
