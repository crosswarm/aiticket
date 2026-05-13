"""
PM 模块数据服务 — 可扩展，不绑定特定 entityType
支持通过 module_key 区分不同 PM 模块（原始需求、规划需求等）

使用方式:
    svc = PMModuleService("original_demand")
    result = svc.fetch_demands(page=1, page_size=30)
    records = result["records"]  # 已展平的需求列表
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import urllib3

# Per-thread storage for current PM user — prevents singleton race under concurrent requests
_pm_user_local = threading.local()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent
_CONFIG_PATH = _BASE_DIR / "config" / "pm_config.yaml"


def _load_config() -> Dict[str, Any]:
    try:
        import yaml
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f).get("pm_system", {})
    except Exception as e:
        print(f"[PMModuleService] 加载配置失败: {e}")
        return {}


class PMModuleService:
    """
    PM 模块通用服务。通过 module_key 参数化，支持多种 PM 模块。
    不在构造函数或方法里硬编码 entityType / endpoint。
    """

    def __init__(self, module_key: str = "original_demand", token_override: str = None, tenant_override: str = None):
        self._config = _load_config()
        modules = self._config.get("modules", {})
        if module_key not in modules:
            raise ValueError(
                f"模块 '{module_key}' 未在 pm_config.yaml 中配置。"
                f"可用模块: {list(modules.keys())}"
            )
        self.module_key = module_key
        self.module = modules[module_key]
        # 支持 env var 覆盖（QCL 通过 PM_BASE_URL 路由到 jira_proxy 转发端点）
        env_base_url = os.environ.get("PM_BASE_URL", "").rstrip("/")
        self.base_url = env_base_url or self._config.get("base_url", "https://pm.example.com")
        self.tenant_info = self._config.get("tenant_info", "0000")
        self.line_id = self._config.get("line_id", "")
        self._token_file = _BASE_DIR / self._config.get("token_file", "data_cache/pm_token.json")
        self._token_override = token_override
        self._tenant_override = tenant_override

    @property
    def current_pm_user(self) -> str | None:
        return getattr(_pm_user_local, 'pm_user', None)

    @current_pm_user.setter
    def current_pm_user(self, value: str | None) -> None:
        _pm_user_local.pm_user = value

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _get_token(self) -> Dict[str, str]:
        """读取 pm_token.json，若不存在则回退到 env var。支持 per-user override。"""
        if self._token_override:
            return {
                "yht_access_token": self._token_override,
                "tenant_info": self._tenant_override or self.tenant_info,
            }
        if self._token_file.exists():
            try:
                with open(self._token_file) as f:
                    return json.load(f)
            except Exception:
                pass
        # Fallback: 环境变量（兼容旧配置）
        token_env = self._config.get("auth", {}).get("token_env", "PM_YHT_ACCESS_TOKEN")
        env_token = os.environ.get(token_env, "")
        if env_token:
            return {"yht_access_token": env_token, "tenant_info": self.tenant_info}
        return {}

    def _refresh_token(self) -> bool:
        """
        自动刷新 PM token：调用 scripts/refresh_pm_token.sh 从 Chrome 重新解密。
        前提：Chrome 已在本机登录 pm.example.com。
        返回 True 表示刷新成功。
        """
        import subprocess
        script = _BASE_DIR / "scripts" / "refresh_pm_token.sh"
        if not script.exists():
            print("[PMModuleService] refresh_pm_token.sh 不存在，跳过自动刷新")
            return False
        try:
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                print("[PMModuleService] PM Token 自动刷新成功")
                return True
            print(f"[PMModuleService] Token 刷新失败: {result.stderr[:200]}")
        except Exception as e:
            print(f"[PMModuleService] Token 刷新异常: {e}")
        return False

    def _build_cookies(self, token_data: Dict) -> Dict[str, str]:
        cookies = {
            "yht_access_token": token_data.get("yht_access_token", ""),
            "tenant_info": token_data.get("tenant_info", self.tenant_info),
        }
        cookies.update(token_data.get("extra_cookies", {}))
        return {k: v for k, v in cookies.items() if v}

    def _base_headers(self) -> Dict[str, str]:
        return {
            "Origin": "https://pm.example.com",
            "Referer": "https://pm.example.com/",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        }

    def _to_direct_pm_url(self, url: str) -> str:
        """将 /pmf_forward 代理 URL 还原为 pm.example.com 直连 URL。"""
        # QCL: http://127.0.0.1:5001/pmf_forward/rest/v1/... → https://pm.example.com/rest/v1/...
        m = re.search(r'/pmf_forward/(.*)', url)
        if m:
            return f"https://pm.example.com/{m.group(1)}"
        # Mini: 已经是 https://pm.example.com/... → 原样
        return url

    def _do_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        执行 HTTP 请求。优先级：
        1. 用户有 wallet cookies + proxy_endpoint → 经 CONNECT 代理路由
        2. 用户有 wallet cookies（无 proxy）→ 直连 pm.example.com，用用户 cookies
        3. 无用户 binding → 默认 Mini token（直连）
        """
        from services.pm_wallet_service import get_user_proxy, get_effective_cookies

        # ---- per-user 路由（有 wallet cookies 即触发） ----
        if self.current_pm_user:
            wallet_cookies = get_effective_cookies(self.current_pm_user)
            if not wallet_cookies.get("yht_access_token"):
                from services.pm_wallet_service import PMNotBoundError
                raise PMNotBoundError(self.current_pm_user)
            if wallet_cookies.get("yht_access_token"):
                real_url = self._to_direct_pm_url(url)
                kwargs["cookies"] = wallet_cookies
                kwargs.setdefault("headers", self._base_headers())
                kwargs.setdefault("verify", False)
                kwargs.setdefault("timeout", 30)

                proxy_ep = get_user_proxy(self.current_pm_user)
                if proxy_ep:
                    kwargs["proxies"] = {"https": proxy_ep, "http": proxy_ep}
                    try:
                        r = requests.request(method, real_url, **kwargs)
                        if r.status_code == 401:
                            logger.warning(f"[pm] user={self.current_pm_user} 401 via proxy {proxy_ep}, 需重新绑定")
                        return r
                    except (requests.ConnectionError, requests.Timeout) as e:
                        logger.warning(f"[pm] user={self.current_pm_user} proxy {proxy_ep} 不可达, 降级直连: {e}")
                        kwargs.pop("proxies", None)

                # 无 proxy 或 proxy 失败：直连（用户 cookies 已在 kwargs）
                r = requests.request(method, real_url, **kwargs)
                if r.status_code == 401:
                    logger.warning(f"[pm] user={self.current_pm_user} 直连 401，需重新绑定 PM session")
                return r

        # ---- 默认路由（无 binding / wallet cookies 为空） ----
        token_data = self._get_token()
        kwargs.setdefault("cookies", self._build_cookies(token_data))
        kwargs.setdefault("headers", self._base_headers())
        kwargs.setdefault("verify", False)
        kwargs.setdefault("timeout", 30)

        r = requests.request(method, url, **kwargs)
        if r.status_code == 401:
            print(f"[PMModuleService] 收到 401，尝试自动刷新 token...")
            if self._refresh_token():
                token_data = self._get_token()
                kwargs["cookies"] = self._build_cookies(token_data)
                r = requests.request(method, url, **kwargs)
        return r

    def _flatten_record(self, record_fields: List[Dict]) -> Dict[str, Any]:
        """
        将 PM API 返回的字段数组格式展平为 dict。
        输入: [{fieldCode, value, title, editType}, ...]
        输出: {fieldCode: display_value, fieldCode_raw: raw_value, ...}
        """
        result: Dict[str, Any] = {}
        for field in record_fields:
            code = field.get("fieldCode")
            if not code:
                continue
            value = field.get("value")
            title = field.get("title")        # 人类可读标签（如"待分析"）
            edit_type = field.get("editType", "")

            if edit_type == "USER" and isinstance(value, dict):
                # 用户字段：保留完整对象 + 提取 display 字段
                result[code] = value
                result[f"{code}_name"] = (
                    value.get("userName") or value.get("name") or ""
                )
            elif edit_type in ("LIST", "MULTI_LIST") and title:
                # 枚举/列表：展示 title，原始值放 _raw
                result[code] = title
                result[f"{code}_raw"] = value
            else:
                result[code] = value
                if title and title != value:
                    result[f"{code}_label"] = title
        return result

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def fetch_demands(
        self,
        page: int = 1,
        page_size: int = 30,
        status_filter: Optional[List[str]] = None,
        extra_conditions: Optional[List[Dict]] = None,
        order_by: str = "ctime",
        is_asc: bool = False,
        fetch_fields: Optional[List[str]] = None,
        assignee_id: Optional[str] = None,
        current_user_only: bool = False,
    ) -> Dict[str, Any]:
        """
        分页查询需求列表。
        - assignee_id: 指定经办人 ID 过滤（如 "0000075951"）
        - current_user_only: True 时自动用 config 里的 default_analyst 过滤
        返回 {"success": bool, "total": N, "records": [...], "page": N, "pages": N}
        """
        token_data = self._get_token()
        if not token_data.get("yht_access_token"):
            return {
                "success": False,
                "message": "PM Token 未配置，请运行 scripts/refresh_pm_token.sh",
                "total": 0, "records": [], "page": page, "pages": 0,
            }

        # current_user_only: 使用 config.default_analyst 作为 assignee 过滤
        effective_assignee = assignee_id
        if current_user_only and not effective_assignee:
            effective_assignee = self._config.get("default_analyst", "")

        conditions: List[Dict] = []
        if self.line_id:
            conditions.append({
                "fieldCode": "lineId",
                "operation": "eq",
                "valueType": "STRING",
                "editType": "LIST",
                "values": [self.line_id],
            })
        if effective_assignee:
            conditions.append({
                "fieldCode": "assignee",
                "operation": "eq",
                "valueType": "STRING",
                "editType": "USER",
                "values": [effective_assignee],
            })
        if status_filter:
            conditions.append({
                "fieldCode": "status",
                "operation": "in",
                "valueType": "STRING",
                "editType": "LIST",
                "values": status_filter,
            })
        # 注入模块级默认过滤条件（来自 pm_config.yaml 的 default_extra_conditions）
        merged_extra = list(self.module.get("default_extra_conditions", []))
        if extra_conditions:
            merged_extra.extend(extra_conditions)
        if merged_extra:
            conditions.extend(merged_extra)

        body = {
            "key": "",
            "lineId": self.line_id,
            "isAsc": is_asc,
            "orderBy": order_by,
            "selfOnly": False,
            "pageNumber": page,
            "pageSize": page_size,
            "entityType": self.module["entity_type"],
            "specific": self.module.get("specific", self.module["entity_type"]),
            "fetchFields": fetch_fields or self.module.get("fetch_fields", ["aid", "code", "title", "status"]),
            "onlyAttention": False,
            "conditions": conditions,
            "conditionGroups": None,
            "queryTotal": True,
        }

        url = f"{self.base_url}{self.module['endpoint']}?tenant_info={self.tenant_info}"

        try:
            r = self._do_request("POST", url, json=body)
            r.raise_for_status()
            data = r.json()
            page_data = data.get("page", {})
            records = [self._flatten_record(rec) for rec in page_data.get("records", [])]
            return {
                "success": True,
                "total": page_data.get("total", 0),
                "page": page_data.get("current", page),
                "page_size": page_data.get("size", page_size),
                "pages": page_data.get("pages", 0),
                "records": records,
            }
        except requests.HTTPError as e:
            snippet = e.response.text[:300] if e.response else ""
            return {"success": False, "message": f"HTTP {e.response.status_code}: {snippet}",
                    "total": 0, "records": [], "page": page, "pages": 0}
        except Exception as e:
            return {"success": False, "message": str(e),
                    "total": 0, "records": [], "page": page, "pages": 0}

    def get_demand_detail(self, aid: str) -> Dict[str, Any]:
        """
        获取单条需求详情（全量字段）。
        优先使用 GET /originalDemand/{aid} 端点（返回所有 53 个字段，无需指定 fetchFields）。
        """
        token_data = self._get_token()
        if not token_data.get("yht_access_token"):
            return {"success": False, "message": "PM Token 未配置", "record": None}

        detail_endpoint = self.module.get("detail_endpoint", "/rest/v1/originalDemand/{aid}")
        url = f"{self.base_url}{detail_endpoint.format(aid=aid)}?tenant_info={self.tenant_info}"
        try:
            r = self._do_request("GET", url)
            r.raise_for_status()
            resp = r.json()
            fields = resp.get("data", [])
            if isinstance(fields, list) and fields:
                record = self._flatten_record(fields)
                # 附件列表（如有）
                record["_attaches"] = resp.get("attaches", [])
                return {"success": True, "record": record}
        except Exception as e:
            pass  # 降级到 page 端点

        # 降级：通过 page 端点过滤 aid
        result = self.fetch_demands(
            page=1, page_size=1,
            extra_conditions=[{"fieldCode": "aid", "operation": "eq",
                               "valueType": "STRING", "values": [aid]}],
        )
        if result["success"] and result["records"]:
            return {"success": True, "record": result["records"][0]}
        return {"success": False, "message": "需求不存在", "record": None}

    def get_process_history(self, aid: str) -> Dict[str, Any]:
        """
        获取需求流程记录（状态变更、评论历史）。
        PM 流程记录端点通过探测发现。
        """
        token_data = self._get_token()
        if not token_data.get("yht_access_token"):
            return {"success": False, "records": [], "message": "PM Token 未配置"}

        # 候选端点（GET 方式），从 module 配置读取，降级到 originalDemand 默认
        _default_history_endpoints = [
            "/rest/v1/originalDemand/trackList?demandId={aid}",
            "/rest/v1/originalDemand/trackList?id={aid}",
            "/rest/v1/originalDemand/history?demandId={aid}",
        ]
        candidates = [
            ep.format(aid=aid)
            for ep in self.module.get("history_endpoints", _default_history_endpoints)
        ]
        for path in candidates:
            url = f"{self.base_url}{path}&tenant_info={self.tenant_info}"
            try:
                r = self._do_request("GET", url, timeout=10)
                if r.ok:
                    resp = r.json()
                    # 尝试从响应中提取记录列表
                    records = resp if isinstance(resp, list) else (
                        resp.get("data") or resp.get("records") or resp.get("list") or []
                    )
                    return {"success": True, "records": records if isinstance(records, list) else []}
            except Exception:
                continue

        return {"success": False, "records": [], "message": "暂无流程记录"}

    def get_available_operations(self, aid: str) -> Dict[str, Any]:
        """
        动态获取需求当前可用的操作列表。
        调用 /rest/v1/workflow/getOperations，返回结果包含每种操作的 operationCode。
        """
        token_data = self._get_token()
        if not token_data.get("yht_access_token"):
            return {"success": False, "operations": [], "message": "PM Token 未配置"}

        params = {
            "aid": aid,
            "entityType": self.module.get("entity_type", "ORIGINAL_DEMAND"),
            "lineId": self.line_id,
            "tenant_info": self.tenant_info,
        }
        try:
            r = self._do_request("GET", f"{self.base_url}/rest/v1/workflow/getOperations", params=params)
            if r.status_code in (401, 403):
                return {"success": False, "operations": [], "error_code": "pm_token_expired", "message": "PM认证已过期"}
            data = r.json() if r.content else []
            ops = data if isinstance(data, list) else []
            return {"success": True, "operations": ops}
        except Exception as e:
            return {"success": False, "operations": [], "message": str(e)}

    def get_process_rules(self, action: str, current_status: str = "WAIT_ANALYSIS") -> Dict[str, Any]:
        """
        获取操作的表单规则（哪些字段必填/可编辑）。
        调用 /rest/v1/workflow/getProcessRules。
        """
        wf = self.module.get("workflow", {})
        action_ops = self.module.get("action_operations", {})
        op_cfg = action_ops.get(action)
        if not op_cfg:
            return {"success": False, "fields": [], "message": f"操作 '{action}' 未配置 action_operations"}

        token_data = self._get_token()
        if not token_data.get("yht_access_token"):
            return {"success": False, "fields": [], "message": "PM Token 未配置"}

        params = {
            "processDesignId": wf.get("process_design_id", ""),
            "linkNode": op_cfg["operation"],
            "sourceNode": current_status,
            "lineId": self._config.get("line_id", ""),
            "entityType": self.module.get("entity_type", "ORIGINAL_DEMAND"),
            "tenant_info": self.tenant_info,
        }
        try:
            r = self._do_request("GET", f"{self.base_url}{wf.get('get_rules_endpoint', '/rest/v1/workflow/getProcessRules')}", params=params)
            if r.status_code in (401, 403):
                return {"success": False, "fields": [], "error_code": "pm_token_expired", "message": "PM认证已过期"}
            data = r.json() if r.content else {}
            fields = data.get("fields", data if isinstance(data, list) else [])
            return {"success": True, "fields": fields}
        except Exception as e:
            return {"success": False, "fields": [], "message": str(e)}

    def execute_action(
        self,
        aid: str,
        action: str,
        payload: Optional[Dict] = None,
        current_status: str = "WAIT_ANALYSIS",
    ) -> Dict[str, Any]:
        """
        执行 PM 原生操作（accept/hang/reject/...）。
        走 workflow 引擎：POST /rest/v1/workflow/processConvert。
        payload.fieldData 包含表单字段值 + aids 列表。
        """
        valid_actions = self.module.get("actions", [])
        if action not in valid_actions:
            return {
                "success": False,
                "message": f"操作 '{action}' 不在模块 '{self.module_key}' 的允许操作列表中: {valid_actions}",
            }

        wf = self.module.get("workflow", {})
        action_ops = self.module.get("action_operations", {})
        op_cfg = action_ops.get(action)
        if not op_cfg:
            return {
                "success": False,
                "message": f"操作 '{action}' 未在 action_operations 中配置",
            }

        token_data = self._get_token()
        if not token_data.get("yht_access_token"):
            return {"success": False, "message": "PM Token 未配置"}

        # 实时探测真实 currentStatus + 匹配真实 operationCode
        # 不信任前端传的 current_status（可能是中文或过期状态），
        # PM 要求 currentStatus 与单据实际状态一致（40010 "单据状态不符" 错）
        configured_op = op_cfg["operation"]
        aliases = op_cfg.get("aliases", [])
        candidate_codes = [configured_op] + list(aliases)
        real_operation = configured_op
        try:
            ops_result = self.get_available_operations(aid)
            if ops_result.get("success") and ops_result.get("operations"):
                # 从候选 operationCode 中找第一个可用的（支持 aliases 兼容不同状态）
                matched = None
                for cand in candidate_codes:
                    for op in ops_result["operations"]:
                        if op.get("operationCode") == cand:
                            matched = op
                            break
                    if matched:
                        break
                if matched:
                    current_status = matched.get("currentStatusCode", current_status)
                    real_operation = matched.get("operationCode", configured_op)
                else:
                    available_codes = [op.get("operationCode") for op in ops_result["operations"]]
                    return {
                        "success": False,
                        "message": f"操作 '{action}' 当前不可用（候选 code: {candidate_codes}）。实际可用: {available_codes}",
                    }
        except Exception as e:
            print(f"[pm_module_service] get_available_operations 失败，用配置默认: {e}")

        # 构建 workflow processConvert 请求
        submit_url = f"{self.base_url}{wf.get('submit_endpoint', '/rest/v1/workflow/processConvert')}"
        params = {
            "lineId": self._config.get("line_id", ""),
            "entityType": self.module.get("entity_type", "ORIGINAL_DEMAND"),
            "operation": real_operation,
            "currentStatus": current_status,
            "tenant_info": self.tenant_info,
        }

        # fieldData 构建：前端传入的 payload 合并 aids
        field_data = (payload or {}).get("fieldData", payload or {})
        if "aids" not in field_data:
            field_data["aids"] = [aid]
        body = {"fieldData": field_data}

        try:
            r = self._do_request("POST", submit_url, params=params, json=body)
            if r.status_code in (401, 403):
                return {
                    "success": False,
                    "error_code": "pm_token_expired",
                    "message": "PM系统认证已过期，请在设置中刷新PM Token",
                    "http_status": r.status_code,
                }
            # PM processConvert 成功时可能返回 true (bare bool) 而非 dict
            # 保护性处理：确保 data 是 dict 才能调 .get()
            raw_data = r.json() if r.content else {}
            if isinstance(raw_data, dict):
                data = raw_data
                code = data.get("code")
                msg = data.get("msg") or data.get("message")
            elif isinstance(raw_data, bool):
                data = {"raw_bool": raw_data}
                code = 200 if raw_data else 500
                msg = "操作成功" if raw_data else "操作失败"
            else:
                data = {"raw": raw_data}
                code = None
                msg = None
            success = r.ok and (code in (200, None, 0, "200") or r.status_code in (200, 204))
            return {
                "success": success,
                "message": msg or ("操作成功" if success else "操作失败"),
                "data": data,
                "http_status": r.status_code,
            }
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                return {
                    "success": False,
                    "error_code": "pm_token_expired",
                    "message": "PM系统认证已过期，请在设置中刷新PM Token",
                    "http_status": e.response.status_code,
                }
            return {"success": False, "message": f"HTTP {e.response.status_code}", "http_status": e.response.status_code}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def check_token_valid(self) -> Dict[str, Any]:
        """快速验证 token 是否有效"""
        result = self.fetch_demands(page=1, page_size=1)
        if result["success"]:
            return {"valid": True, "total": result["total"],
                    "updated_at": self._get_token().get("updated_at")}
        return {"valid": False, "message": result.get("message")}

    def get_module_info(self) -> Dict[str, Any]:
        """返回当前模块的配置信息（供前端动态渲染）"""
        return {
            "module_key": self.module_key,
            "label": self.module.get("label", self.module_key),
            "entity_type": self.module["entity_type"],
            "statuses": self.module.get("statuses", {}),
            "actions": self.module.get("actions", []),
            "triage_target_statuses": self.module.get("triage_target_statuses", []),
        }


# ------------------------------------------------------------------
# 单例工厂 — 按 module_key 缓存实例
# ------------------------------------------------------------------
_instances: Dict[str, PMModuleService] = {}


def get_pm_module_service(module_key: str = "original_demand") -> PMModuleService:
    """获取（或创建）指定模块的服务单例"""
    if module_key not in _instances:
        inst = PMModuleService(module_key)
        _instances[module_key] = inst
        # PM 系统所有模块共享同一个 pm_token.json，按系统粒度注册一次即可
        try:
            from services.session_keepalive_service import get_keepalive_manager
            mgr = get_keepalive_manager()
            if mgr.get_session_status("pm") is None:
                mgr.register(
                    name="pm",
                    label="PM系统",
                    refresh_fn=inst._refresh_token,
                    interval_minutes=25,
                )
        except Exception as e:
            print(f"[PMModuleService] 会话保活注册失败（不影响功能）: {e}")
    return _instances[module_key]
