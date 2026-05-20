from __future__ import annotations

from typing import List, Optional

from .base import GenericIssue


class JiraIssueProvider:
    """Adapter wrapping the existing JiraService.

    Does NOT modify jira_service.py — only converts JiraIssue → GenericIssue.
    The full Jira fallback chain (direct → proxy → local_cache) remains inside
    BoardService._fetch_board_issues and is not touched by this provider.

    This provider is primarily used by the incremental index script and
    health-check endpoints, NOT by _fetch_board_issues itself for Jira.
    """

    name = "jira"

    def __init__(self, jira_service):
        self._svc = jira_service

    # ------------------------------------------------------------------
    # IssueProvider protocol
    # ------------------------------------------------------------------

    def fetch_all(self, filter_spec: dict | None = None) -> List[GenericIssue]:
        jql = (filter_spec or {}).get("jql") or self._build_default_jql()
        result = self._svc.search_issues_rest_api(jql)
        if "error" in result:
            return []
        return [self._to_generic(i) for i in self._svc.parse_search_response(result)]

    def fetch_incremental(self, since: str) -> List[GenericIssue]:
        jql = f'updated >= "{since}" ORDER BY updated DESC'
        return self.fetch_all({"jql": jql})

    def get_issue(self, key: str) -> Optional[GenericIssue]:
        issues = self.fetch_all({"jql": f'key = "{key}"'})
        return issues[0] if issues else None

    def health_check(self) -> dict:
        result = self._svc.search_issues_rest_api("ORDER BY updated DESC", max_results=1)
        return {"ok": "error" not in result, "source": "jira"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_default_jql(self) -> str:
        try:
            return self._svc.board_config.get("jql", "ORDER BY updated DESC")
        except Exception:
            return "ORDER BY updated DESC"

    @staticmethod
    def _to_generic(ji) -> GenericIssue:
        return GenericIssue(
            source="jira",
            external_id=ji.key,
            key=ji.key,
            summary=ji.summary,
            description=ji.description,
            status=ji.status,
            assignee=ji.assignee,
            reporter=ji.reporter,
            created=ji.created,
            updated=ji.updated,
            priority=ji.priority,
            issue_type=ji.issue_type,
            project_name=ji.project_name,
            extra={
                "contact_name": ji.contact_name,
                "contact_info": ji.contact_info,
                "customer_name": ji.customer_name,
                "product_version": ji.product_version,
                "deploy_mode": ji.deploy_mode,
                "due_date": ji.due_date,
            },
        )
