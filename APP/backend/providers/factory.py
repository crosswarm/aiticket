from __future__ import annotations

from config.loader import cfg

from .base import IssueProvider


def get_active_provider() -> IssueProvider | None:
    """Return the configured IssueProvider, or None when type is 'jira'.

    Kept in its own module to avoid circular imports: config/loader.py is a
    pure config module with no business-logic imports.  This factory is called
    from main.py at startup after all modules are loaded.

    Returns None for the jira type so that BoardService.__init__ can detect
    "use the native Jira path" and leave _issue_provider as None.
    """
    data_source_cfg = cfg("data_source") or {}
    source_type = data_source_cfg.get("type", "jira")

    if source_type == "jira":
        return None  # BoardService uses its existing Jira fallback chain

    if source_type == "excel":
        from .excel_provider import ExcelIssueProvider
        excel_cfg = cfg("data_source", "excel") or {}
        return ExcelIssueProvider(
            file_path=excel_cfg.get("file_path", "data/imports/tickets.xlsx"),
            column_map=excel_cfg.get("column_map") or {},
        )

    if source_type == "api":
        from .api_provider import GenericAPIIssueProvider
        return GenericAPIIssueProvider(cfg("data_source", "api") or {})

    raise ValueError(
        f"Unknown data_source.type: '{source_type}'. "
        f"Supported values: jira, excel, api"
    )
