# AITicket Data Source Providers

The `providers/` package lets you connect AITicket to any issue source — not just Jira.  
Configure the active source in `config/deployment.yaml` (see `samples/deployment.example.yaml`).

## Supported sources

| Type | Class | Use case |
|------|-------|----------|
| `jira` (default) | `JiraIssueProvider` | Connected Jira instance |
| `excel` | `ExcelIssueProvider` | `.xlsx`, `.xls`, or `.csv` file |
| `api` | `GenericAPIIssueProvider` | Any JSON REST API |

---

## Quick start: Excel

1. Copy `data/imports/sample_tickets.xlsx` as a template.
2. Add the following to `config/deployment.yaml`:

```yaml
data_source:
  type: excel
  excel:
    file_path: "data/imports/my_tickets.xlsx"
    column_map:
      key:          "工单号"     # required — must be unique, no colons
      summary:      "标题"
      description:  "问题描述"
      status:       "状态"
      assignee:     "处理人"
      created:      "创建时间"
      # Extra columns are stored in extra and accessible as attributes:
      customer_name: "客户名称"
```

3. Start the server.  The board will load issues from the spreadsheet.

**Notes**
- `key` column values must not contain colons.
- Extra columns in `column_map` (keys not in the standard field list) are stored in
  `GenericIssue.extra` and accessible as `issue.customer_name`, `issue.my_field`, etc.
- `fetch_incremental` re-reads the entire file (static snapshots have no delta).

---

## Quick start: Third-party REST API

```yaml
data_source:
  type: api
  api:
    base_url: "https://support.example.com"
    auth:
      type: bearer
      token_env: "SUPPORT_API_TOKEN"   # set this env var before starting
    endpoints:
      list:   "/api/v2/tickets"
      detail: "/api/v2/tickets/{id}"   # optional
    pagination:
      style:      page
      page_param: page
      size_param: per_page
      size:       100
    items_key: "$.tickets"   # JSON path to the array in the response
    field_map:
      external_id: "$.id"
      summary:     "$.subject"
      description: "$.description"
      status:      "$.status"
      assignee:    "$.assignee_id"
      created:     "$.created_at"
      updated:     "$.updated_at"
```

**Field map path syntax**: `$.field` or `$.nested.field` (dot notation).  
**Pagination styles**: `page` (page number), `offset` (record offset), `none` (single request).  
**Auth types**: `bearer`, `basic` (`username_env` / `password_env`), `apikey` (`key_env` / `header`).

---

## Architecture

```
IssueProvider (Protocol)
  ├── JiraIssueProvider     wraps JiraService, converts JiraIssue → GenericIssue
  ├── ExcelIssueProvider    reads spreadsheet via pandas
  └── GenericAPIIssueProvider  fetches JSON REST API with pagination

GenericIssue
  • Standard fields: key, summary, description, status, assignee, ...
  • extra dict: source-specific fields
  • __getattr__ fallback: issue.contact_name reads from extra, so all
    existing callers in board_service_chroma.py work without changes

IssueRepository (non-Jira only)
  • Caches GenericIssue list as data/cache/{source}_board.json
  • Atomic write-then-rename prevents partial reads

BoardService integration
  • Jira path: unchanged, uses native JiraService fallback chain
  • Non-Jira path: activated when issue_provider.name != 'jira'
```

---

## Adding a new source

1. Create `providers/my_source.py` with a class that implements:
   - `name: str`
   - `fetch_all(filter_spec=None) -> List[GenericIssue]`
   - `fetch_incremental(since: str) -> List[GenericIssue]`
   - `get_issue(key: str) -> Optional[GenericIssue]`
   - `health_check() -> dict`
2. Register it in `providers/factory.py` under a new `source_type` string.
3. Add the config section to `deployment.yaml`.
