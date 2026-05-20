from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol


@dataclass
class GenericIssue:
    """Cross-source normalized issue model.

    Fields mirror JiraIssue's public surface so existing callers in
    board_service_chroma.py that access `issue.contact_name` etc. still work
    via the __getattr__ fallback to `extra`.
    """

    source: str           # "jira" | "excel" | "api" | ...
    external_id: str      # original ID within the source
    key: str              # globally unique: bare for Jira, "excel:<id>" for others
    summary: str
    description: str = ""
    status: str = ""
    assignee: str = ""
    reporter: str = ""
    created: str = ""
    updated: str = ""
    priority: str = ""
    issue_type: str = ""
    project_name: str = ""
    extra: dict = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        # Allow callers that use JiraIssue-style attribute access (.contact_name,
        # .customer_name, .due_date, etc.) to work without any changes.
        try:
            return self.extra[name]
        except KeyError:
            return ""


class IssueProvider(Protocol):
    name: str

    def fetch_all(self, filter_spec: dict | None = None) -> List[GenericIssue]: ...
    def fetch_incremental(self, since: str) -> List[GenericIssue]: ...
    def get_issue(self, key: str) -> Optional[GenericIssue]: ...
    def health_check(self) -> dict: ...
