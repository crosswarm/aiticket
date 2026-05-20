from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .base import GenericIssue

# Fields that are first-class attributes on GenericIssue; anything else in
# column_map becomes an extra key.
_STANDARD_FIELDS = {
    "key", "summary", "description", "status", "assignee",
    "reporter", "created", "updated", "priority", "issue_type", "project_name",
}


class ExcelIssueProvider:
    """Import issues from an Excel (.xlsx/.xls) or CSV file.

    Configure via deployment.yaml:

        data_source:
          type: excel
          excel:
            file_path: "data/imports/tickets.xlsx"
            column_map:
              key: "工单号"          # required; value must not contain colons
              summary: "标题"
              description: "问题描述"
              status: "状态"
              assignee: "处理人"
              created: "创建时间"

    Any column_map entry whose key is not in the standard field list is stored
    in GenericIssue.extra and accessible via attribute access (.my_field).

    Requires: pandas, openpyxl (for .xlsx files)
    """

    name = "excel"

    def __init__(self, file_path: str, column_map: dict):
        self.file_path = str(file_path)
        self.column_map = column_map or {}

    # ------------------------------------------------------------------
    # IssueProvider protocol
    # ------------------------------------------------------------------

    def fetch_all(self, filter_spec: dict | None = None) -> List[GenericIssue]:
        import pandas as pd

        path = Path(self.file_path)
        if not path.exists():
            print(f"[ExcelProvider] file not found: {self.file_path}")
            return []

        ext = path.suffix.lower()
        try:
            df = pd.read_excel(path, dtype=str) if ext in (".xlsx", ".xls") else pd.read_csv(path, dtype=str)
        except Exception as e:
            print(f"[ExcelProvider] failed to read {self.file_path}: {e}")
            return []

        df = df.fillna("")
        key_col = self.column_map.get("key", "")
        if not key_col or key_col not in df.columns:
            print(f"[ExcelProvider] key column '{key_col}' not found in {list(df.columns)}")
            return []

        issues: List[GenericIssue] = []
        for _, row in df.iterrows():
            ext_id = str(row[key_col]).strip()
            if not ext_id or ext_id.lower() == "nan":
                continue

            def _get(field_name: str) -> str:
                col = self.column_map.get(field_name, "")
                if col and col in df.columns:
                    return str(row[col]).strip()
                return ""

            extra = {
                k: str(row[v]).strip()
                for k, v in self.column_map.items()
                if k not in _STANDARD_FIELDS and v in df.columns
            }

            issues.append(GenericIssue(
                source="excel",
                external_id=ext_id,
                key=f"excel:{ext_id}",
                summary=_get("summary"),
                description=_get("description"),
                status=_get("status"),
                assignee=_get("assignee"),
                reporter=_get("reporter"),
                created=_get("created"),
                updated=_get("updated"),
                priority=_get("priority"),
                issue_type=_get("issue_type"),
                project_name=_get("project_name"),
                extra=extra,
            ))

        print(f"[ExcelProvider] loaded {len(issues)} issues from {self.file_path}")
        return issues

    def fetch_incremental(self, since: str) -> List[GenericIssue]:
        # Excel files are static snapshots; re-read the entire file.
        return self.fetch_all()

    def get_issue(self, key: str) -> Optional[GenericIssue]:
        ext_id = key.removeprefix("excel:")
        for issue in self.fetch_all():
            if issue.external_id == ext_id:
                return issue
        return None

    def health_check(self) -> dict:
        ok = Path(self.file_path).is_file()
        return {"ok": ok, "source": "excel", "file": self.file_path}
