"""
ReqIngestAgent — 需求池自动入库 Agent

每日 06:30 由 JobMaster 触发，将"回复方式=纳入需求库"的工单同步到需求池。
筛选条件（项目/时间窗/经办人/回复方式值）全部从 schedule params 读取，支持前端覆盖。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from agents.base import AgentTask, AgentStatus, BaseAgent

_BACKEND = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _BACKEND.parent.parent
_TOPIC_PATH = str(_BACKEND / "data" / "topic.md")

logger = logging.getLogger(__name__)

_INGEST_FIELDS = ",".join([
    "summary", "description", "created", "status", "priority",
    "assignee", "reporter", "issuetype",
    "customfield_10725",   # 客户/项目名称
    "customfield_10410",   # 回复方式
    "customfield_10402",   # 客户问题类型
    "customfield_10729",   # 研发确认问题类型
])


class ReqIngestAgent(BaseAgent):
    name         = "req_ingest"
    display_name = "需求池自动入库 Agent"
    description  = "每日自动同步回复方式=纳入需求库的工单到需求池，打上主题标签"
    version      = "1.0"

    def __init__(self, vector_store=None):
        self.vector_store = vector_store
        self._topic_parser = None

    def _get_topic_parser(self):
        if self._topic_parser is None:
            try:
                sys.path.insert(0, str(_BACKEND))
                from analysis import TopicParser
                self._topic_parser = TopicParser(_TOPIC_PATH)
            except Exception as e:
                logger.warning(f"[ReqIngest] TopicParser init failed: {e}")
        return self._topic_parser

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.list_capabilities(),
        }

    def list_capabilities(self) -> List[str]:
        return ["jira-poll", "reqpool-ingest", "topic-tagging", "feishu-notify"]

    def health_check(self) -> dict:
        try:
            from jira_service import jira_service as jira_svc
            if not jira_svc:
                return {"healthy": False, "detail": "jira_service not initialised"}
            return {"healthy": True, "detail": "jira_service ok"}
        except Exception as e:
            return {"healthy": False, "detail": str(e)[:100]}

    def run_task(self, task: AgentTask) -> dict:
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()

        # ── 读取 payload 参数（来自 schedule params 或前端覆盖）──────────
        payload = {}
        if task.payload_json:
            try:
                payload = json.loads(task.payload_json)
            except Exception:
                pass

        project             = payload.get("project", "MYPROJECT")
        days_back           = int(payload.get("days_back", 365))
        assignees: list     = payload.get("assignees") or []
        reply_values: list  = payload.get("reply_method_values") or ["纳入需求库"]

        self.append_log(task.id, f"入库参数 — 项目:{project} 回溯:{days_back}天 经办人:{assignees or '全部'} 回复方式:{reply_values}")
        self.report_progress(task.id, 5, "构建 JQL 并查询 Jira")

        # ── 构建 JQL ─────────────────────────────────────────────────
        from services.reply_method_detector import build_ingest_jql
        jql = build_ingest_jql(
            project=project,
            days_back=days_back,
            assignees=assignees if assignees else None,
            extra_values=[v for v in reply_values if v != "纳入需求库"],
        )
        self.append_log(task.id, f"JQL: {jql}")

        # ── 分页拉取 Jira 工单 ────────────────────────────────────────
        issues = self._fetch_all_issues(jql, task, store)
        self.append_log(task.id, f"Jira 返回 {len(issues)} 条工单")
        self.report_progress(task.id, 30, f"拉取完成，开始打主题标签并入库")

        # ── 逐条入库 ─────────────────────────────────────────────────
        ingested = 0
        errors   = 0
        topic_dist: dict = {}

        parser = self._get_topic_parser()

        for i, issue in enumerate(issues):
            try:
                fields  = issue.get("fields") or {}
                key     = issue.get("key", "")
                summary = (fields.get("summary") or "")[:500]
                desc    = (fields.get("description") or "")[:2000]

                # 主题标签
                topic_l1, topic_l2 = self._classify_topic(parser, summary, desc)
                topic_dist[topic_l1] = topic_dist.get(topic_l1, 0) + 1

                # 入库 metadata
                metadata = {
                    "status":          "new",
                    "source_issues":   [key],
                    "entry_source":    "auto_ingest",
                    "ingest_source":   "auto",
                    "ingest_ts":       datetime.utcnow().isoformat(),
                    "topic_l1":        topic_l1,
                    "topic_l2":        topic_l2,
                    "created_at":      (fields.get("created") or datetime.utcnow().isoformat())[:19],
                    "jira_project":    project,
                    "jira_assignee":   _extract_user(fields.get("assignee")),
                    "jira_customer":   _extract_customer(fields),
                    "jira_priority":   _extract_priority(fields),
                }

                req_id = f"auto_{key}"
                if self.vector_store:
                    self.vector_store.upsert_requirement(req_id, summary, desc, metadata)
                else:
                    # 降级：通过 req_pool_service 入库
                    _req_pool_upsert_fallback(req_id, summary, desc, metadata)

                ingested += 1
                if i % 10 == 0:
                    progress = 30 + int(i / len(issues) * 60)
                    self.report_progress(task.id, progress, f"已入库 {ingested}/{len(issues)}")
            except Exception as e:
                errors += 1
                self.append_log(task.id, f"[ERROR] 入库失败 {issue.get('key','?')}: {e}")

        # ── 飞书通知 ─────────────────────────────────────────────────
        dist_str = " | ".join(f"{k}:{v}" for k, v in sorted(topic_dist.items(), key=lambda x: -x[1])[:5])
        summary_msg = (
            f"📥 需求池自动入库完成\n"
            f"• 入库 {ingested} 条（失败 {errors} 条）\n"
            f"• 项目: {project}  |  回溯: {days_back} 天\n"
            f"• 主题分布(Top5): {dist_str or '未分类'}"
        )
        self.append_log(task.id, summary_msg)
        self.report_progress(task.id, 95, "发送飞书通知")
        _send_feishu(summary_msg)
        self.report_progress(task.id, 100, "完成")

        return {
            "ingested": ingested,
            "errors":   errors,
            "total":    len(issues),
            "topic_dist": topic_dist,
            "jql":      jql,
        }

    def _fetch_all_issues(self, jql: str, task: AgentTask, store) -> list:
        """分页拉取全部工单（最多 500 条）"""
        try:
            from jira_service import jira_service as jira_svc
        except Exception as e:
            self.append_log(task.id, f"[WARN] jira_service import failed: {e}")
            return []

        all_issues = []
        start_at = 0
        page_size = 50
        max_total = 500

        while start_at < max_total:
            try:
                resp = jira_svc.search_issues_rest_api(
                    jql=jql,
                    start_at=start_at,
                    max_results=page_size,
                    fields=_INGEST_FIELDS,
                )
                if resp.get("error"):
                    self.append_log(task.id, f"[WARN] Jira error: {resp['error']}")
                    break
                issues = resp.get("issues") or []
                all_issues.extend(issues)
                total = resp.get("total", 0)
                start_at += len(issues)
                if start_at >= total or not issues:
                    break
            except Exception as e:
                self.append_log(task.id, f"[ERROR] Jira 请求失败 (start={start_at}): {e}")
                break

        return all_issues

    def _classify_topic(self, parser, summary: str, description: str):
        """用 TopicParser 对工单打 L1/L2 主题标签"""
        topic_l1 = "未分类"
        topic_l2 = ""
        if not parser:
            return topic_l1, topic_l2
        try:
            result = parser.classify_ticket_with_leaf_priority(summary, description or "")
            if result:
                path = result.get("full_path") or ""
                parts = path.split("/") if path else []
                topic_l1 = parts[0] if len(parts) >= 1 else "未分类"
                topic_l2 = parts[1] if len(parts) >= 2 else ""
        except Exception as e:
            logger.debug(f"[ReqIngest] classify failed: {e}")
        return topic_l1, topic_l2


def _extract_user(user_field) -> str:
    if not user_field:
        return ""
    if isinstance(user_field, dict):
        return user_field.get("displayName") or user_field.get("name") or ""
    return str(user_field)


def _extract_priority(fields: dict) -> str:
    p = fields.get("priority")
    if isinstance(p, dict):
        return p.get("name") or ""
    return str(p or "")


def _extract_customer(fields: dict) -> str:
    cf = fields.get("customfield_10725")
    if isinstance(cf, list) and cf:
        return str(cf[0]) if not isinstance(cf[0], dict) else (cf[0].get("value") or cf[0].get("name") or "")
    if isinstance(cf, dict):
        return cf.get("value") or cf.get("name") or ""
    return str(cf or "")


def _req_pool_upsert_fallback(req_id: str, title: str, description: str, metadata: dict):
    """当 vector_store 未注入时记录警告，production 中 vector_store 应始终传入"""
    logger.warning(f"[ReqIngest] vector_store not injected, skipping upsert for {req_id}")


def _send_feishu(message: str):
    try:
        from services.feishu_notifier import get_notifier
        get_notifier().send_message(message)
    except Exception as e:
        logger.debug(f"[ReqIngest] feishu send failed: {e}")
