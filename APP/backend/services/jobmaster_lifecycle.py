"""
JobMasterLifecycle — Agent 生命周期管理核心（图书馆员模型）

职责：
  - 编目（catalog）：domain / category / capabilities 三维归类，形成体系
  - 复用优先（find_reusable）：spawn 前先检查相似 agent，Jaccard ≥ 0.7 推荐复用
  - token 签发 / 校验（随机 token + SHA-256 hash 存 DB，60s LRU cache）
  - Agent 注册表（agent_registry 表，同 agent_tasks.db）
  - 事件日志（data/jobmaster_events.jsonl，append-only，>10MB 自动轮转）
  - 状态机推进 + 父子树约束
  - 模式切换：AUDIT（仅记录）↔ ENFORCE（未授权直接拒绝）

状态机：
  pending → running → completed / failed / suspended / revoked
  suspended → running（resume）
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── 路径 ─────────────────────────────────────────────────────────────────────
_BACKEND    = Path(__file__).resolve().parent.parent
_DB_PATH    = _BACKEND / "data" / "sqlite" / "agent_tasks.db"
_EVENTS_LOG = _BACKEND / "data" / "jobmaster_events.jsonl"
_STATE_FILE = _BACKEND / "data" / "jobmaster_state.json"

_EVENTS_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB 轮转阈值
_TOKEN_CACHE_TTL = 60.0                    # token 验证 LRU cache TTL（秒）
_CATALOG_CACHE_TTL = 30.0                  # catalog 缓存 TTL（秒）

# ─── Domain 归类映射表 ─────────────────────────────────────────────────────────
# 格式：ClassName -> (domain, category, capabilities[])
# domain:   项目主题域（每个 domain 有 root agent）
# category: domain（主题专属）/ functional（公共职能）/ system（系统级）
DOMAIN_MAPPING: Dict[str, Tuple[str, str, List[str]]] = {
    "ReplyAgent":           ("reply",     "domain",     ["smart_reply", "style_learning", "feedback_ingest", "kb_search"]),
    "KbFactAgent":          ("kb",        "domain",     ["kb_search", "kb_ingest", "fact_extraction"]),
    "AdoptedAgent":         ("kb",        "functional", ["adoption_tracking", "fact_status_update"]),
    "DarwinAgent":          ("evolution", "system",     ["evolution_eval", "strategy_optimization", "ratchet"]),
    "CompetitorAgent":      ("research",  "functional", ["competitor_research", "product_analysis", "screenshot"]),
    "HandoverSuggestAgent": ("reply",     "functional", ["handover", "routing", "assignment"]),
}

# ─── 调度中心 DDL（base schema；新列通过 ALTER TABLE 安全添加）───────────────────
_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS agent_registry (
    agent_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    parent_agent_id TEXT,
    state           TEXT NOT NULL DEFAULT 'pending',
    token_hash      TEXT NOT NULL,
    token_scope     TEXT NOT NULL DEFAULT '["create:subagent","read:own","write:own"]',
    resource_budget TEXT NOT NULL DEFAULT '{"max_subagents":5,"max_llm_calls_per_hour":200}',
    subagent_count  INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_heartbeat  TEXT,
    revoked_at      TEXT,
    revoke_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ar_parent ON agent_registry(parent_agent_id);
CREATE INDEX IF NOT EXISTS idx_ar_state  ON agent_registry(state);
CREATE INDEX IF NOT EXISTS idx_ar_name   ON agent_registry(name);
"""

# 新增列定义（ALTER TABLE 安全添加，已存在时静默跳过）
_NEW_COLUMNS = [
    ("domain",       "TEXT"),
    ("category",     "TEXT"),
    ("capabilities", "TEXT DEFAULT '[]'"),
    ("purpose",      "TEXT"),
]

# ─── 合法状态 ──────────────────────────────────────────────────────────────────
VALID_STATES   = {"pending", "running", "completed", "failed", "suspended", "revoked"}
TERMINAL_STATES = {"completed", "failed", "revoked"}

# ─── JobMaster 根 agent 保留 ID ─────────────────────────────────────────────────
JOBMASTER_AGENT_ID = "jobmaster_root"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class JobMasterLifecycle:
    """
    单例。main.py 启动时调用 JobMasterLifecycle.get_instance().bootstrap() 完成初始化。

    角色定位：图书馆员（curator/librarian）
      - 复用优先：spawn 前调 find_reusable；命中阈值时建议复用而非新建
      - 主题归类：domain / category / capabilities 三维体系
      - 建议而非阻断：advisory 模式通知，仅明确违规才 deny
    """
    _instance: Optional["JobMasterLifecycle"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._db_path      = str(_DB_PATH)
        self._write_lock   = threading.Lock()
        self._spawn_lock   = threading.Lock()          # 防并发 spawn 竞争
        self._mode         = "audit"
        self._root_token: Optional[str] = None
        # token LRU cache: token_hash -> (result_dict | None, expire_monotonic)
        self._token_cache: Dict[str, Tuple[Optional[Dict], float]] = {}
        self._token_cache_lock = threading.Lock()
        # catalog cache: (catalog_dict, expire_monotonic)
        self._catalog_cache: Optional[Tuple[Dict, float]] = None

        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 单例 ──────────────────────────────────────────────────────────────────
    @classmethod
    def get_instance(cls) -> "JobMasterLifecycle":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── 初始化 ─────────────────────────────────────────────────────────────────
    def _init_db(self):
        """建表并安全添加新列（幂等，可重入）。"""
        with sqlite3.connect(self._db_path) as con:
            con.executescript(_REGISTRY_DDL)
            con.commit()
            # 检查并添加新列（S7：已存在列静默跳过）
            existing_cols = {row[1] for row in con.execute("PRAGMA table_info(agent_registry)").fetchall()}
            for col, defn in _NEW_COLUMNS:
                if col not in existing_cols:
                    try:
                        con.execute(f"ALTER TABLE agent_registry ADD COLUMN {col} {defn}")
                    except sqlite3.OperationalError:
                        pass  # 列已存在，忽略
            con.commit()

    def bootstrap(self) -> str:
        """
        进程启动时调用。确保 jobmaster_root 存在，加载模式设置，
        自动归类已有 agent，返回 root token。
        """
        state = self._read_state()
        root_token = state.get("root_token")

        with sqlite3.connect(self._db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT agent_id, state FROM agent_registry WHERE agent_id=?",
                (JOBMASTER_AGENT_ID,)
            ).fetchone()

            if row is None:
                if not root_token:
                    root_token = f"jm_root_{secrets.token_hex(32)}"
                token_hash = _hash_token(root_token)
                con.execute(
                    """INSERT INTO agent_registry
                       (agent_id,name,parent_agent_id,state,token_hash,token_scope,
                        resource_budget,subagent_count,created_by,created_at,
                        domain,category,capabilities,purpose)
                       VALUES (?,?,NULL,'running',?,?,?,0,'bootstrap',?,?,?,?,?)""",
                    (
                        JOBMASTER_AGENT_ID, "jobmaster",
                        token_hash,
                        json.dumps(["*"]),
                        json.dumps({"max_subagents": 999, "max_llm_calls_per_hour": 9999}),
                        _now_iso(),
                        "system", "system",
                        json.dumps(["*"]),
                        "JobMaster 调度中心根节点",
                    )
                )
                con.commit()
                state["root_token"] = root_token
                self._write_state(state)
                self._write_event(
                    actor=JOBMASTER_AGENT_ID, agent_id=JOBMASTER_AGENT_ID,
                    event_type="bootstrap", risk_level="low", decision="auto_approved",
                    context={"mode": self._mode},
                )
            else:
                if not root_token:
                    root_token = f"jm_root_{secrets.token_hex(32)}"
                    token_hash = _hash_token(root_token)
                    con.execute(
                        "UPDATE agent_registry SET token_hash=? WHERE agent_id=?",
                        (token_hash, JOBMASTER_AGENT_ID)
                    )
                    con.commit()
                    state["root_token"] = root_token
                    self._write_state(state)
                # 补填 domain/category/capabilities（ALTER 后的旧记录可能为 NULL）
                con.execute(
                    """UPDATE agent_registry
                       SET domain='system', category='system',
                           capabilities=?, purpose='JobMaster 调度中心根节点'
                       WHERE agent_id=? AND domain IS NULL""",
                    (json.dumps(["*"]), JOBMASTER_AGENT_ID)
                )
                con.commit()

        self._mode = state.get("lifecycle_mode", "audit")
        self._root_token = root_token

        # 自动归类已有 agent（S2/S8：幂等，仅更新 domain IS NULL 的行）
        try:
            n = self.auto_categorize_existing_agents()
            if n:
                print(f"[JobMaster] auto-categorized {n} agents")
        except Exception as e:
            print(f"[JobMaster] auto-categorize failed (non-fatal): {e}")

        print(f"[JobMaster] bootstrap OK — mode={self._mode}")
        return root_token

    # ── Token 签发 ──────────────────────────────────────────────────────────────
    def issue_token(
        self,
        agent_id: str,
        scope: Optional[List[str]] = None,
        created_by: str = JOBMASTER_AGENT_ID,
    ) -> str:
        token = f"jm_{agent_id}_{secrets.token_hex(24)}"
        token_hash = _hash_token(token)
        scope_json = json.dumps(scope or ["create:subagent", "read:own", "write:own"])
        with sqlite3.connect(self._db_path) as con:
            con.execute(
                "UPDATE agent_registry SET token_hash=?, token_scope=? WHERE agent_id=?",
                (token_hash, scope_json, agent_id)
            )
            con.commit()
        self._invalidate_token_cache(token_hash)
        return token

    def issue_system_token(self, agent_class_name: str) -> str:
        """
        main.py 启动时为已知 agent 签发系统 token。
        自动从 DOMAIN_MAPPING 填 domain / category / capabilities。
        """
        agent_id = f"agt_sys_{agent_class_name}_{secrets.token_hex(6)}"
        token = f"jm_{agent_id}_{secrets.token_hex(24)}"
        token_hash = _hash_token(token)

        mapping = DOMAIN_MAPPING.get(agent_class_name)
        domain     = mapping[0] if mapping else None
        category   = mapping[1] if mapping else None
        caps_json  = json.dumps(mapping[2]) if mapping else "[]"

        with sqlite3.connect(self._db_path) as con:
            con.execute(
                """INSERT OR REPLACE INTO agent_registry
                   (agent_id,name,parent_agent_id,state,token_hash,token_scope,
                    resource_budget,subagent_count,created_by,created_at,
                    domain,category,capabilities,purpose)
                   VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                (
                    agent_id, agent_class_name, JOBMASTER_AGENT_ID,
                    "running", token_hash,
                    json.dumps(["create:subagent", "read:own", "write:own"]),
                    json.dumps({"max_subagents": 5, "max_llm_calls_per_hour": 200}),
                    "system_bootstrap", _now_iso(),
                    domain, category, caps_json,
                    f"{agent_class_name} 系统 agent（自动注册）",
                )
            )
            con.commit()
        self._write_event(
            actor=JOBMASTER_AGENT_ID, agent_id=agent_id,
            event_type="spawn_approved", risk_level="low", decision="auto_approved",
            context={"name": agent_class_name, "domain": domain, "source": "system_bootstrap"},
        )
        self._invalidate_catalog_cache()
        return token

    # ── Token 校验（带 60s LRU cache）─────────────────────────────────────────
    def verify_token(self, token: str, require_scope: Optional[str] = None) -> Optional[Dict]:
        if not token:
            return None
        token_hash = _hash_token(token)
        now = time.monotonic()

        # 查缓存
        with self._token_cache_lock:
            cached = self._token_cache.get(token_hash)
            if cached and now < cached[1]:
                row_dict = cached[0]
                # 仍需做 scope 校验（缓存只存 base 行）
                if row_dict is None:
                    return None
                if require_scope:
                    scopes = json.loads(row_dict.get("token_scope") or "[]")
                    if "*" not in scopes and require_scope not in scopes:
                        return None
                return row_dict

        # 未命中 → 查 DB
        with sqlite3.connect(self._db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM agent_registry WHERE token_hash=?", (token_hash,)
            ).fetchone()

        result = dict(row) if row else None
        if result and result["state"] in TERMINAL_STATES and result["agent_id"] != JOBMASTER_AGENT_ID:
            result = None

        # 写缓存
        with self._token_cache_lock:
            self._token_cache[token_hash] = (result, now + _TOKEN_CACHE_TTL)

        if result is None:
            return None
        if require_scope:
            scopes = json.loads(result.get("token_scope") or "[]")
            if "*" not in scopes and require_scope not in scopes:
                return None
        return result

    # ── 复用查找 ──────────────────────────────────────────────────────────────
    def find_reusable(
        self,
        domain: str,
        capabilities: List[str],
        threshold: float = 0.7,
    ) -> Optional[Dict]:
        """
        在同 domain 内找能力相似度 ≥ threshold 的 running/pending agent。
        返回完整 reasoning 供上层 LLM 判断，不强制复用。
        S3: 仅考虑 running / pending 状态（排除 suspended / failed / revoked）。
        """
        caps_set = set(capabilities)
        with sqlite3.connect(self._db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM agent_registry WHERE domain=? AND state IN ('running','pending')",
                (domain,)
            ).fetchall()

        best: Optional[Dict] = None
        best_score = 0.0
        for row in rows:
            row_caps = set(json.loads(row["capabilities"] or "[]"))
            union = row_caps | caps_set
            if not union:
                continue
            score = len(row_caps & caps_set) / len(union)
            if score >= threshold and score > best_score:
                best_score = score
                best = dict(row)

        if best:
            return {
                "reused": True,
                "agent_id": best["agent_id"],
                "name": best["name"],
                "domain": best.get("domain"),
                "state": best["state"],
                "overlap_score": round(best_score, 3),
                "candidate_capabilities": json.loads(best.get("capabilities") or "[]"),
                "requested_capabilities": list(capabilities),
                "advisory": (
                    f"已有 agent '{best['name']}' 提供相似能力"
                    f"（相似度 {round(best_score * 100)}%），建议复用"
                ),
            }
        return None

    # ── Catalog（调度中心视图，30s cache）─────────────────────────────────────
    def catalog(self) -> Dict:
        """
        返回 {domain: {category: [agent_summary...]}} 树形视图。
        30s in-memory cache，spawn / revoke 时自动 invalidate。
        """
        now = time.monotonic()
        if self._catalog_cache and now < self._catalog_cache[1]:
            return self._catalog_cache[0]

        agents = [a for a in self.list_agents() if a.get("state") != "revoked"]
        result: Dict[str, Dict[str, List]] = {}
        for a in agents:
            domain   = a.get("domain") or "uncategorized"
            category = a.get("category") or "unknown"
            result.setdefault(domain, {}).setdefault(category, []).append({
                "agent_id":        a["agent_id"],
                "name":            a["name"],
                "state":           a["state"],
                "capabilities":    json.loads(a.get("capabilities") or "[]"),
                "purpose":         a.get("purpose") or "",
                "parent_agent_id": a.get("parent_agent_id"),
                "subagent_count":  a.get("subagent_count", 0),
            })

        self._catalog_cache = (result, now + _CATALOG_CACHE_TTL)
        return result

    # ── Domain Root 注册 ──────────────────────────────────────────────────────
    def register_domain_root(
        self, domain: str, agent_id: str, requester_token: str
    ) -> bool:
        """
        指定 agent_id 为某 domain 的 root agent。
        S11：仅 jobmaster_root token 可调，普通 token 返回 False（403）。
        """
        caller = self.verify_token(requester_token)
        if not caller or caller["agent_id"] != JOBMASTER_AGENT_ID:
            return False
        with sqlite3.connect(self._db_path) as con:
            con.execute(
                "UPDATE agent_registry SET domain=?, category='domain' WHERE agent_id=?",
                (domain, agent_id)
            )
            con.commit()
        self._invalidate_catalog_cache()
        self._write_event(
            actor=JOBMASTER_AGENT_ID, agent_id=agent_id,
            event_type="domain_root_registered", risk_level="low", decision="auto_approved",
            context={"domain": domain},
        )
        return True

    # ── 自动归类 ──────────────────────────────────────────────────────────────
    def auto_categorize_existing_agents(self) -> int:
        """
        对 domain IS NULL 的行按 DOMAIN_MAPPING 自动填充。
        S2/S8：幂等；已归类的行不覆写。
        """
        count = 0
        with sqlite3.connect(self._db_path) as con:
            rows = con.execute(
                "SELECT agent_id, name FROM agent_registry WHERE domain IS NULL AND agent_id != ?",
                (JOBMASTER_AGENT_ID,)
            ).fetchall()
            for agent_id, name in rows:
                mapping = DOMAIN_MAPPING.get(name)
                if mapping:
                    domain, category, capabilities = mapping
                    con.execute(
                        "UPDATE agent_registry SET domain=?, category=?, capabilities=? WHERE agent_id=?",
                        (domain, category, json.dumps(capabilities), agent_id)
                    )
                    count += 1
            if count:
                con.commit()
        if count:
            self._invalidate_catalog_cache()
        return count

    # ── Spawn（复用优先）──────────────────────────────────────────────────────
    def spawn(
        self,
        agent_class_name: str,
        parent_token: Optional[str] = None,
        scope: Optional[List[str]] = None,
        context: Optional[Dict] = None,
        domain: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        purpose: Optional[str] = None,
    ) -> Dict:
        """
        申请创建 agent（图书馆员模型）。

        流程：
          1. 有 domain + capabilities → 先 find_reusable（Jaccard ≥ 0.7）
          2. 命中 → 返回 {reused:True, ...advisory} 建议复用，不新建
          3. 未命中 → 新建，自动挂到 domain root 下
        """
        # S4/P8：Lock 串行化，防并发竞争
        with self._spawn_lock:
            return self._spawn_inner(
                agent_class_name=agent_class_name,
                parent_token=parent_token,
                scope=scope,
                context=context,
                domain=domain,
                capabilities=capabilities or [],
                purpose=purpose,
            )

    def _spawn_inner(
        self,
        agent_class_name: str,
        parent_token: Optional[str],
        scope: Optional[List[str]],
        context: Optional[Dict],
        domain: Optional[str],
        capabilities: List[str],
        purpose: Optional[str],
    ) -> Dict:
        # 1) 复用检查
        if domain and capabilities:
            reuse = self.find_reusable(domain, capabilities)
            if reuse:
                self._write_event(
                    actor=JOBMASTER_AGENT_ID, agent_id=reuse["agent_id"],
                    event_type="reuse_recommended", risk_level="low", decision="advisory",
                    context={
                        "requested_class": agent_class_name,
                        "overlap_score": reuse["overlap_score"],
                        "advisory": reuse["advisory"],
                    },
                )
                return {
                    "agent_id": reuse["agent_id"],
                    "token": None,
                    "approved": True,
                    "reused": True,
                    "reuse_recommendation": reuse,
                    "reason": reuse["advisory"],
                }

        # 2) 风险评估
        caller = self._resolve_caller(parent_token)
        risk_level, deny_reason = self._assess_spawn_risk(caller, agent_class_name, scope)

        if self._mode == "enforce" and deny_reason:
            self._write_event(
                actor=caller["agent_id"] if caller else "unknown",
                agent_id="?", event_type="spawn_denied",
                risk_level=risk_level, decision="denied",
                context={"agent_class": agent_class_name, "reason": deny_reason, **(context or {})},
            )
            return {"agent_id": None, "token": None, "approved": False, "reused": False, "reason": deny_reason}

        if deny_reason and self._mode == "audit":
            self._write_event(
                actor=caller["agent_id"] if caller else "unknown",
                agent_id="?", event_type="spawn_audit_warn",
                risk_level=risk_level, decision="audit_allowed",
                context={"agent_class": agent_class_name, "warn": deny_reason, **(context or {})},
                notify_user=(risk_level == "high"),
            )

        # 3) 从 DOMAIN_MAPPING 补充 domain/capabilities
        mapping = DOMAIN_MAPPING.get(agent_class_name)
        if not domain and mapping:
            domain    = mapping[0]
            category  = mapping[1]
            if not capabilities:
                capabilities = mapping[2]
        else:
            category = mapping[1] if mapping else None

        # 4) 创建新 agent
        agent_id = (
            f"agt_{agent_class_name}_"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_"
            f"{secrets.token_hex(4)}"
        )
        token = f"jm_{agent_id}_{secrets.token_hex(24)}"
        token_hash = _hash_token(token)
        parent_id = caller["agent_id"] if caller else None
        purpose_clean = self._sanitize_purpose(purpose)

        with sqlite3.connect(self._db_path) as con:
            con.execute(
                """INSERT INTO agent_registry
                   (agent_id,name,parent_agent_id,state,token_hash,token_scope,
                    resource_budget,subagent_count,created_by,created_at,
                    domain,category,capabilities,purpose)
                   VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                (
                    agent_id, agent_class_name, parent_id,
                    "pending", token_hash,
                    json.dumps(scope or ["create:subagent", "read:own", "write:own"]),
                    json.dumps({"max_subagents": 5, "max_llm_calls_per_hour": 200}),
                    parent_id or "external", _now_iso(),
                    domain, category, json.dumps(capabilities), purpose_clean,
                )
            )
            if parent_id:
                con.execute(
                    "UPDATE agent_registry SET subagent_count=subagent_count+1 WHERE agent_id=?",
                    (parent_id,)
                )
            con.commit()

        self._write_event(
            actor=parent_id or "external", agent_id=agent_id,
            event_type="spawn_approved", risk_level=risk_level, decision="auto_approved",
            context={"name": agent_class_name, "domain": domain, "parent": parent_id, **(context or {})},
        )
        self._invalidate_catalog_cache()

        return {
            "agent_id": agent_id, "token": token,
            "approved": True, "reused": False,
            "reuse_recommendation": None, "reason": None,
        }

    # ── 状态推进 ───────────────────────────────────────────────────────────────
    def transition_state(self, agent_id: str, token: str, new_state: str) -> bool:
        if new_state not in VALID_STATES:
            return False
        caller = self.verify_token(token)
        if not caller or caller["agent_id"] != agent_id:
            return False
        with sqlite3.connect(self._db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT state FROM agent_registry WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if not row or row["state"] in TERMINAL_STATES:
                return False
            con.execute(
                "UPDATE agent_registry SET state=? WHERE agent_id=?", (new_state, agent_id)
            )
            con.commit()
        self._write_event(
            actor=agent_id, agent_id=agent_id,
            event_type="state_transition", risk_level="low", decision="auto_approved",
            context={"from": row["state"], "to": new_state},
        )
        return True

    def heartbeat(self, agent_id: str, token: str) -> bool:
        caller = self.verify_token(token)
        if not caller or caller["agent_id"] != agent_id:
            return False
        with sqlite3.connect(self._db_path) as con:
            con.execute(
                "UPDATE agent_registry SET last_heartbeat=? WHERE agent_id=?",
                (_now_iso(), agent_id)
            )
            con.commit()
        return True

    def revoke(self, agent_id: str, reason: str = "", requester_token: Optional[str] = None) -> bool:
        if requester_token:
            caller = self.verify_token(requester_token, require_scope="revoke:any")
            if not caller:
                return False
        with sqlite3.connect(self._db_path) as con:
            con.execute(
                "UPDATE agent_registry SET state='revoked',revoked_at=?,revoke_reason=? WHERE agent_id=?",
                (_now_iso(), reason, agent_id)
            )
            con.commit()
        self._invalidate_catalog_cache()
        self._write_event(
            actor=requester_token and "requester" or JOBMASTER_AGENT_ID,
            agent_id=agent_id, event_type="revoke",
            risk_level="high", decision="jobmaster_approved",
            context={"reason": reason}, notify_user=True,
        )
        return True

    # ── Hook 授权检查（advisory 模式）─────────────────────────────────────────
    def authorize_claude_task(self, session_id: str, description: str, subagent_type: str) -> Dict:
        """
        Claude Code PreToolUse(Task) hook 调用此接口。
        返回 {allow: True, mode, advisory?}
        advisory 字段存在时，Hook 打印建议但不阻断（P5: fail-open）。
        """
        advisory: Optional[str] = None

        # 尝试从 description 解析 domain + capabilities，给出复用建议
        if description and subagent_type:
            # 简单关键词推断 domain
            domain_hints = {
                "reply": ["回复", "reply", "smart_reply", "reply_agent"],
                "kb": ["知识库", "kb", "knowledge"],
                "research": ["竞品", "competitor", "research"],
                "evolution": ["进化", "evolution", "darwin"],
            }
            detected_domain = None
            for d, hints in domain_hints.items():
                if any(h.lower() in description.lower() for h in hints):
                    detected_domain = d
                    break

            if detected_domain:
                reuse = self.find_reusable(detected_domain, [])
                if reuse:
                    advisory = reuse["advisory"]

        self._write_event(
            actor="claude_code",
            agent_id=f"cc_{session_id[:8]}",
            event_type="claude_task_request",
            risk_level="low",
            decision="audit_allowed" if self._mode == "audit" else "auto_approved",
            context={
                "session_id": session_id,
                "description": description,
                "subagent_type": subagent_type,
                "mode": self._mode,
                "advisory": advisory,
            },
        )
        result: Dict = {"allow": True, "mode": self._mode}
        if advisory:
            result["advisory"] = advisory
        return result

    # ── 巡查 ───────────────────────────────────────────────────────────────────
    def audit_scan(self) -> Dict:
        from datetime import timedelta
        issues = []
        now = datetime.now(timezone.utc)

        with sqlite3.connect(self._db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM agent_registry WHERE state='running'"
            ).fetchall()

        for row in rows:
            if row["agent_id"] == JOBMASTER_AGENT_ID:
                continue
            if row["last_heartbeat"]:
                try:
                    hb = datetime.fromisoformat(
                        row["last_heartbeat"].rstrip("Z")
                    ).replace(tzinfo=timezone.utc)
                    if (now - hb) > timedelta(minutes=30):
                        issues.append({
                            "type": "zombie_agent",
                            "agent_id": row["agent_id"],
                            "last_heartbeat": row["last_heartbeat"],
                        })
                        with sqlite3.connect(self._db_path) as w:
                            w.execute(
                                "UPDATE agent_registry SET state='suspended' WHERE agent_id=?",
                                (row["agent_id"],)
                            )
                            w.commit()
                        self._write_event(
                            actor=JOBMASTER_AGENT_ID, agent_id=row["agent_id"],
                            event_type="zombie_suspended",
                            risk_level="medium", decision="jobmaster_approved",
                            context={"last_heartbeat": row["last_heartbeat"]},
                        )
                except Exception:
                    pass

            budget = json.loads(row["resource_budget"] or "{}")
            max_sub = budget.get("max_subagents", 5)
            if row["subagent_count"] > max_sub:
                issues.append({
                    "type": "budget_breach",
                    "agent_id": row["agent_id"],
                    "subagent_count": row["subagent_count"],
                    "max_subagents": max_sub,
                })

        if issues:
            self._invalidate_catalog_cache()

        return {"scanned_at": _now_iso(), "issues": issues, "issue_count": len(issues)}

    # ── 查询 ───────────────────────────────────────────────────────────────────
    def list_agents(self, state_filter: Optional[str] = None) -> List[Dict]:
        with sqlite3.connect(self._db_path) as con:
            con.row_factory = sqlite3.Row
            if state_filter:
                rows = con.execute(
                    "SELECT * FROM agent_registry WHERE state=? ORDER BY created_at DESC",
                    (state_filter,)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM agent_registry ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_agent(self, agent_id: str) -> Optional[Dict]:
        with sqlite3.connect(self._db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM agent_registry WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_events(self, limit: int = 100, agent_id: Optional[str] = None) -> List[Dict]:
        events = []
        try:
            with open(_EVENTS_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        if agent_id is None or e.get("agent_id") == agent_id:
                            events.append(e)
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
        return events[-limit:]

    # ── 模式切换 ───────────────────────────────────────────────────────────────
    def set_mode(self, mode: str) -> bool:
        if mode not in ("audit", "enforce"):
            return False
        self._mode = mode
        state = self._read_state()
        state["lifecycle_mode"] = mode
        self._write_state(state)
        self._write_event(
            actor=JOBMASTER_AGENT_ID, agent_id=JOBMASTER_AGENT_ID,
            event_type="mode_changed", risk_level="high", decision="jobmaster_approved",
            context={"new_mode": mode}, notify_user=True,
        )
        print(f"[JobMaster] mode switched to {mode}")
        return True

    @property
    def mode(self) -> str:
        return self._mode

    # ── 内部工具 ───────────────────────────────────────────────────────────────
    def _resolve_caller(self, parent_token: Optional[str]) -> Optional[Dict]:
        if not parent_token:
            return None
        return self.verify_token(parent_token, require_scope="create:subagent")

    def _assess_spawn_risk(
        self, caller: Optional[Dict], agent_class_name: str, scope: Optional[List[str]]
    ) -> Tuple[str, Optional[str]]:
        if caller is None:
            return "medium", "no_valid_parent_token"
        if caller.get("state") in TERMINAL_STATES:
            return "high", "parent_revoked"
        budget = json.loads(caller.get("resource_budget") or "{}")
        max_sub = budget.get("max_subagents", 5)
        if caller.get("subagent_count", 0) >= max_sub:
            return "medium", f"parent_budget_exceeded:{caller['agent_id']}"
        if caller["agent_id"] != JOBMASTER_AGENT_ID:
            scopes = json.loads(caller.get("token_scope") or "[]")
            if "*" not in scopes and "create:subagent" not in scopes:
                return "high", "insufficient_scope"
        return "low", None

    def _invalidate_token_cache(self, token_hash: Optional[str] = None) -> None:
        with self._token_cache_lock:
            if token_hash:
                self._token_cache.pop(token_hash, None)
            else:
                self._token_cache.clear()

    def _invalidate_catalog_cache(self) -> None:
        self._catalog_cache = None

    @staticmethod
    def _sanitize_purpose(purpose: Optional[str]) -> str:
        """S6：限长 500，过滤换行符。"""
        if not purpose:
            return ""
        return purpose.replace("\n", " ").replace("\r", " ")[:500]

    def _maybe_rotate_events_log(self) -> None:
        """P4：JSONL >10MB 自动轮转。"""
        try:
            if _EVENTS_LOG.exists() and _EVENTS_LOG.stat().st_size > _EVENTS_LOG_MAX_BYTES:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                rotated = _EVENTS_LOG.with_name(f"jobmaster_events.{ts}.jsonl")
                _EVENTS_LOG.rename(rotated)
                print(f"[JobMaster] events log rotated to {rotated.name}")
        except Exception as exc:
            print(f"[JobMaster] events log rotation failed: {exc}")

    def _write_event(
        self,
        actor: str,
        agent_id: str,
        event_type: str,
        risk_level: str,
        decision: str,
        context: Optional[Dict] = None,
        notify_user: bool = False,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
    ) -> None:
        import secrets as _secrets
        event = {
            "event_id": f"evt_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{_secrets.token_hex(4)}",
            "ts": _now_iso(),
            "actor": actor,
            "agent_id": agent_id,
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "risk_level": risk_level,
            "decision": decision,
            "context": context or {},
            "notify_user": notify_user,
            "feishu_message_id": None,
        }
        with self._write_lock:
            self._maybe_rotate_events_log()
            try:
                with open(_EVENTS_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception as exc:
                print(f"[JobMaster] event log write failed: {exc}")

        if notify_user:
            self._notify_user_async(event)

    def _notify_user_async(self, event: Dict) -> None:
        def _send():
            try:
                from services.feishu_notifier import FeishuNotifier
                notifier = FeishuNotifier()
                level_emoji = {"low": "ℹ️", "medium": "⚠️", "high": "🚨"}.get(event["risk_level"], "📋")
                msg = (
                    f"{level_emoji} **[JobMaster] {event['event_type']}**\n"
                    f"Agent: `{event['agent_id']}`\n"
                    f"风险: {event['risk_level']} | 决策: {event['decision']}\n"
                    f"详情: {json.dumps(event['context'], ensure_ascii=False)[:200]}"
                )
                notifier.send_message(msg)
            except Exception as exc:
                print(f"[JobMaster] feishu notify failed: {exc}")
        threading.Thread(target=_send, daemon=True).start()

    def _read_state(self) -> Dict:
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, state: Dict) -> None:
        try:
            _STATE_FILE.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as exc:
            print(f"[JobMaster] state write failed: {exc}")
