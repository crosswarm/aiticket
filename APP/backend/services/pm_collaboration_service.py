"""
PM 协作任务服务
提供 PM 系统 API 封装、预定义协作管理、自动处理逻辑
"""

import json
import os
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path

import requests

from models.pm_models import (
    PMDemand,
    PredefineData,
    ProcessResult,
    ProcessAction,
    PredefineMatch,
    PMBoardStats,
    DemandStatus,
    PredefineStatus,
)


class PMCollaborationService:
    """PM协作任务服务"""

    def __init__(self, config: Optional[Dict[str, Any]] = None, token_override: str = None, tenant_override: str = None):
        self.config = config or self._load_config()
        self.base_url = self.config.get("base_url", "https://pm.example.com")
        self.api_prefix = self.config.get("api_prefix", "/rest/v1")
        self.tenant_info = self.config.get("tenant_info", "0000")
        self.line_id = self.config.get("line_id", "")
        self.default_analyst = self.config.get("default_analyst", "")

        # 认证信息: per-user override 优先
        self._token_override = token_override
        self._tenant_override = tenant_override
        self.token = token_override or os.environ.get("PM_YHT_ACCESS_TOKEN", "")

        # 缓存目录
        self.cache_dir = Path(self.config.get("cache_dir", "data_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.demands_cache_file = self.cache_dir / "pm_demands.json"
        self.predefines_file = self.cache_dir / "pm_predefines.json"
        self.process_history_file = self.cache_dir / "pm_process_history.json"

        # 初始化预定义管理器和自动处理器
        self.predefine_manager = PMPredefineManager(self.predefines_file)
        self.auto_processor = PMAutoProcessor(self)

        # 会话
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://pm.example.com",
            "Referer": "https://pm.example.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })

    def _load_config(self) -> Dict[str, Any]:
        """加载配置"""
        config_file = Path("APP/backend/config/pm_config.yaml")
        if config_file.exists():
            try:
                import yaml
                with open(config_file, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                    return config.get("pm_system", {})
            except Exception as e:
                print(f"[PMService] 加载配置失败: {e}")

        # 默认配置
        return {
            "base_url": "https://pm.example.com",
            "api_prefix": "/rest/v1",
            "tenant_info": "0000",
            "line_id": "3058614d-5e02-45b3-8084-33d4c6e6a49b",
            "default_analyst": "0000075951",
            "cache_dir": "data_cache",
        }

    def _get_cookies(self) -> Dict[str, str]:
        """获取认证cookies, per-user override 优先"""
        return {
            "tenant_info": self._tenant_override or self.tenant_info,
            "yht_access_token": self._token_override or self.token,
        }

    def fetch_demands(
        self,
        status_list: Optional[List[str]] = None,
        page_number: int = 1,
        page_size: int = 100,
    ) -> List[PMDemand]:
        """
        从PM系统获取需求列表

        Args:
            status_list: 状态列表，如 ["WAIT_ANALYSIS", "COO_ACCEPT"]
            page_number: 页码
            page_size: 每页数量

        Returns:
            需求列表
        """
        url = f"{self.base_url}{self.api_prefix}/demand/page"

        # 构建请求体
        conditions = []

        # 产线条件
        if self.line_id:
            conditions.append({
                "fieldCode": "lineId",
                "operation": "eq",
                "valueType": "STRING",
                "values": [self.line_id],
            })

        # 分析人条件
        if self.default_analyst:
            conditions.append({
                "fieldCode": "analyst",
                "operation": "eq",
                "valueType": "STRING",
                "values": [self.default_analyst],
            })

        # 状态条件
        if status_list:
            conditions.append({
                "fieldCode": "status",
                "operation": "in",
                "valueType": "STRING",
                "values": status_list,
            })

        # 未关闭的条件
        conditions.append({
            "fieldCode": "closeTime",
            "operation": "ey",
            "valueType": "DATE",
            "values": [],
        })

        payload = {
            "pageNumber": page_number,
            "pageSize": page_size,
            "isAsc": False,
            "orderBy": "ctime",
            "selfOnly": False,
            "entityType": "DEMAND",
            "specific": "DEMAND",
            "withPermission": False,
            "fetchFields": [
                "aid", "code", "title", "status", "analyst",
                "productId", "categoryId", "corProposer",
                "expectedResolveTime", "commitDeliveryTime", "ctime", "mtime",
                "closeTime", "description", "priority",
            ],
            "conditions": conditions,
        }

        try:
            response = self.session.post(
                url,
                cookies=self._get_cookies(),
                json=payload,
                timeout=30,
            )
            response.raise_for_status()

            data = response.json()
            if data.get("code") != "0":
                print(f"[PMService] API返回错误: {data.get('message')}")
                return []

            items = data.get("data", {}).get("list", [])
            demands = []
            for item in items:
                try:
                    demand = PMDemand.from_api_response(item)
                    demands.append(demand)
                except Exception as e:
                    print(f"[PMService] 解析需求失败: {e}")

            return demands

        except requests.RequestException as e:
            print(f"[PMService] 请求失败: {e}")
            return []
        except Exception as e:
            print(f"[PMService] 未知错误: {e}")
            return []

    def get_pending_demands(self) -> List[PMDemand]:
        """获取待分析的需求列表"""
        return self.fetch_demands(status_list=["WAIT_ANALYSIS"])

    def sync_to_cache(self, demands: List[PMDemand]) -> Dict[str, int]:
        """
        同步需求到本地缓存

        Returns:
            {"total": 总数, "new": 新增, "updated": 更新}
        """
        # 加载现有缓存
        existing = self._load_cached_demands()
        existing_dict = {d.aid: d for d in existing}

        new_count = 0
        updated_count = 0

        for demand in demands:
            if demand.aid not in existing_dict:
                new_count += 1
            else:
                existing_demand = existing_dict[demand.aid]
                if demand.update_time != existing_demand.update_time:
                    updated_count += 1
                # 保留本地处理字段
                demand.processed_at = existing_demand.processed_at
                demand.processed_action = existing_demand.processed_action
                demand.predefine_id = existing_demand.predefine_id
                demand.reject_reason = existing_demand.reject_reason

            existing_dict[demand.aid] = demand

        # 保存
        all_demands = list(existing_dict.values())
        self._save_cached_demands(all_demands)

        return {
            "total": len(all_demands),
            "new": new_count,
            "updated": updated_count,
        }

    def _load_cached_demands(self) -> List[PMDemand]:
        """加载缓存的需求"""
        if not self.demands_cache_file.exists():
            return []

        try:
            with open(self.demands_cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [PMDemand(**item) for item in data]
        except Exception as e:
            print(f"[PMService] 加载缓存失败: {e}")
            return []

    def _save_cached_demands(self, demands: List[PMDemand]):
        """保存需求到缓存"""
        try:
            with open(self.demands_cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    [d.model_dump() for d in demands],
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
        except Exception as e:
            print(f"[PMService] 保存缓存失败: {e}")

    def get_cached_demands(
        self,
        status: Optional[DemandStatus] = None,
        overdue_only: bool = False,
    ) -> List[PMDemand]:
        """获取缓存的需求列表"""
        demands = self._load_cached_demands()

        if status:
            demands = [d for d in demands if d.status == status]

        if overdue_only:
            demands = [d for d in demands if d.is_overdue]

        return demands

    def get_stats(self) -> PMBoardStats:
        """获取看板统计数据"""
        demands = self._load_cached_demands()

        today = datetime.now().date()

        stats = PMBoardStats()
        stats.total = len(demands)
        stats.wait_analysis = len([d for d in demands if d.status == DemandStatus.WAIT_ANALYSIS])
        stats.coo_accept = len([d for d in demands if d.status == DemandStatus.COO_ACCEPT])
        stats.coo_hang = len([d for d in demands if d.status == DemandStatus.COO_HANG])
        stats.overdue = len([d for d in demands if d.is_overdue])

        # 今日处理
        processed_today = [
            d for d in demands
            if d.processed_at and d.processed_at.date() == today
        ]
        stats.processed_today = len(processed_today)
        stats.auto_processed_today = len([
            d for d in processed_today
            if d.processed_by == "system"
        ])

        return stats

    def update_demand_process_status(
        self,
        demand_id: str,
        action: ProcessAction,
        predefine_id: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        """更新需求的处理状态"""
        demands = self._load_cached_demands()

        for demand in demands:
            if demand.aid == demand_id:
                demand.processed_at = datetime.now()
                demand.processed_action = action.value
                demand.processed_by = "system"
                demand.predefine_id = predefine_id
                demand.reject_reason = reason
                break

        self._save_cached_demands(demands)


class PMPredefineManager:
    """预定义协作管理器"""

    def __init__(self, storage_file: Path):
        self.storage_file = storage_file
        self.predefines: List[PredefineData] = []
        self._load()

    def _load(self):
        """加载预定义数据"""
        if not self.storage_file.exists():
            self.predefines = []
            return

        try:
            with open(self.storage_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.predefines = [PredefineData(**item) for item in data]
        except Exception as e:
            print(f"[PredefineManager] 加载失败: {e}")
            self.predefines = []

    def _save(self):
        """保存预定义数据"""
        try:
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump(
                    [p.model_dump() for p in self.predefines],
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
        except Exception as e:
            print(f"[PredefineManager] 保存失败: {e}")

    def create(self, data: Dict[str, Any]) -> PredefineData:
        """创建预定义"""
        predefine = PredefineData(**data)
        self.predefines.append(predefine)
        self._save()
        return predefine

    def list_all(self, active_only: bool = True) -> List[PredefineData]:
        """列出所有预定义"""
        if active_only:
            return [p for p in self.predefines if p.is_active]
        return self.predefines

    def get(self, predefine_id: str) -> Optional[PredefineData]:
        """获取指定预定义"""
        for p in self.predefines:
            if p.id == predefine_id:
                return p
        return None

    def delete(self, predefine_id: str) -> bool:
        """删除预定义"""
        for i, p in enumerate(self.predefines):
            if p.id == predefine_id:
                self.predefines.pop(i)
                self._save()
                return True
        return False

    def match_demand(self, demand: PMDemand) -> Optional[PredefineMatch]:
        """
        匹配需求与预定义

        匹配算法:
        1. 提出人名称匹配 (权重 40%)
        2. 领域/部门匹配 (权重 30%)
        3. 关键词匹配 (权重 30%)
        """
        best_match = None
        best_score = 0.0
        MATCH_THRESHOLD = 0.7

        for predefine in self.predefines:
            if not predefine.is_active:
                continue

            score = 0.0
            matched_fields = []

            # 1. 提出人匹配 (40%)
            proposer_name = demand.cor_proposer.name if demand.cor_proposer else ""
            if proposer_name and proposer_name == predefine.proposer_name:
                score += 0.4
                matched_fields.append("proposer_name")

            # 2. 领域匹配 (30%)
            proposer_dept = demand.cor_proposer.dept if demand.cor_proposer else ""
            if predefine.proposer_domain and proposer_dept:
                if predefine.proposer_domain in proposer_dept:
                    score += 0.3
                    matched_fields.append("proposer_domain")

            # 3. 关键词匹配 (30%)
            if predefine.keywords and demand.title:
                title = demand.title.lower()
                matched_keywords = [
                    k for k in predefine.keywords
                    if k.lower() in title
                ]
                if matched_keywords:
                    score += 0.3 * (len(matched_keywords) / len(predefine.keywords))
                    matched_fields.append("keywords")

            # 更新最佳匹配
            if score > best_score and score >= MATCH_THRESHOLD:
                best_score = score
                best_match = PredefineMatch(
                    predefine=predefine,
                    score=score,
                    matched_fields=matched_fields,
                )

        return best_match


class PMAutoProcessor:
    """PM自动处理器"""

    # 拒绝消息模板
    REJECT_TEMPLATE = """尊敬的 {proposer_name}：

感谢您提交的协作需求《{demand_title}》。

经评估，该需求暂不符合当前平台部的接收条件：
- 需求内容与当前规划方向不匹配
- 或资源安排已满，无法承诺交付时间

建议：
1. 联系平台产品规划部（强骁）确认需求优先级
2. 通过正式需求评审流程提交
3. 或联系相关业务线负责人协调资源

感谢您的理解与支持！

—— 平台产品规划部（自动回复）
"""

    def __init__(self, service: PMCollaborationService):
        self.service = service

    def process_demand(self, demand: PMDemand) -> ProcessResult:
        """
        自动处理需求

        处理逻辑:
        1. 尝试匹配预定义
        2. 匹配成功 -> 自动采纳
        3. 匹配失败 -> 自动拒绝
        4. 异常情况 -> 需人工处理
        """
        # 只处理待分析状态的需求
        if demand.status != DemandStatus.WAIT_ANALYSIS:
            return ProcessResult(
                demand_id=demand.aid,
                action=ProcessAction.SKIP,
                reason=f"状态为 {demand.status.value}，非待分析状态",
            )

        # 已处理过的不重复处理
        if demand.processed_at:
            return ProcessResult(
                demand_id=demand.aid,
                action=ProcessAction.SKIP,
                reason="已处理过",
            )

        try:
            # 尝试匹配预定义
            match = self.service.predefine_manager.match_demand(demand)

            if match and match.predefine.auto_accept:
                # 自动采纳
                return self._accept_demand(demand, match)
            else:
                # 自动拒绝
                return self._reject_demand(demand, match)

        except Exception as e:
            return ProcessResult(
                demand_id=demand.aid,
                action=ProcessAction.MANUAL,
                reason="自动处理异常",
                success=False,
                error_message=str(e),
            )

    def _accept_demand(
        self,
        demand: PMDemand,
        match: PredefineMatch,
    ) -> ProcessResult:
        """自动采纳需求"""
        # TODO: 调用PM系统API进行采纳操作
        # 目前只更新本地状态

        self.service.update_demand_process_status(
            demand_id=demand.aid,
            action=ProcessAction.ACCEPT,
            predefine_id=match.predefine.id,
            reason=f"匹配预定义: {match.predefine.display_text} (匹配度: {match.score:.2f})",
        )

        return ProcessResult(
            demand_id=demand.aid,
            action=ProcessAction.ACCEPT,
            predefine_id=match.predefine.id,
            reason=f"匹配预定义: {match.predefine.display_text}",
        )

    def _reject_demand(
        self,
        demand: PMDemand,
        match: Optional[PredefineMatch] = None,
    ) -> ProcessResult:
        """自动拒绝需求"""
        proposer_name = demand.cor_proposer.name if demand.cor_proposer else "用户"

        reject_reason = self.REJECT_TEMPLATE.format(
            proposer_name=proposer_name,
            demand_title=demand.title,
        )

        # TODO: 调用PM系统API进行拒绝操作
        # 目前只更新本地状态

        self.service.update_demand_process_status(
            demand_id=demand.aid,
            action=ProcessAction.REJECT,
            predefine_id=match.predefine.id if match else None,
            reason=reject_reason,
        )

        return ProcessResult(
            demand_id=demand.aid,
            action=ProcessAction.REJECT,
            predefine_id=match.predefine.id if match else None,
            reason="不符合预定义规则，自动拒绝",
        )

    def process_all_pending(self) -> List[ProcessResult]:
        """处理所有待分析的需求"""
        demands = self.service.get_pending_demands()
        results = []

        for demand in demands:
            result = self.process_demand(demand)
            if result.action != ProcessAction.SKIP:
                results.append(result)

        return results


class PMOverdueMonitor:
    """PM超时监控器"""

    def __init__(self, service: PMCollaborationService):
        self.service = service
        self.threshold_days = 2

    def check_overdue_demands(self) -> List[PMDemand]:
        """检查超时的需求"""
        demands = self.service.get_cached_demands()

        overdue = []
        for demand in demands:
            # 只检查待分析状态的需求
            if demand.status != DemandStatus.WAIT_ANALYSIS:
                continue

            # 已处理过的不检查
            if demand.processed_at:
                continue

            if demand.is_overdue:
                overdue.append(demand)

        return overdue

    def get_overdue_stats(self) -> Dict[str, Any]:
        """获取超时统计"""
        overdue = self.check_overdue_demands()

        # 按等待天数分组
        groups = {}
        for d in overdue:
            days = d.waiting_days
            if days not in groups:
                groups[days] = []
            groups[days].append(d)

        return {
            "total": len(overdue),
            "groups": {
                f"{k}天": len(v) for k, v in sorted(groups.items())
            },
            "demands": overdue,
        }


# 单例实例
_pm_service: Optional[PMCollaborationService] = None


def get_pm_service() -> PMCollaborationService:
    """获取PM服务单例"""
    global _pm_service
    if _pm_service is None:
        _pm_service = PMCollaborationService()
    return _pm_service
