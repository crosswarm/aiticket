# 第三方 REST API 数据源接入手册

AITicket 支持从任意 REST API 读取工单数据，无需 Jira 账号。适合：

- 已有 Zendesk、Freshdesk、Linear、Intercom 等工单系统
- 内部自研工单系统
- 任何能通过 HTTP GET 返回 JSON 列表的数据接口

---

## 快速开始

### 1. 确认 API 接口

你需要一个能返回工单列表的 HTTP 接口，响应格式为 JSON。例如：

```http
GET https://api.example.com/v2/tickets
Authorization: Bearer <token>

Response:
{
  "tickets": [
    {"id": 12345, "subject": "无法登录", "description": "点击登录按钮无响应", "status": "open"},
    ...
  ]
}
```

### 2. 配置 deployment.yaml

```yaml
instance:
  name: "我的工单系统"
  slug: "myticket"
  primary_project_key: "TICKET"

data_source:
  type: api
  api:
    base_url: "https://api.example.com"
    auth:
      type: bearer
      token_env: "SUPPORT_API_TOKEN"
    endpoints:
      list: "/v2/tickets"
    field_map:
      external_id: "$.id"
      summary:     "$.subject"
      description: "$.description"
      status:      "$.status"
    response_path: "$.tickets"
```

### 3. 设置认证凭据

在 `.env` 文件中添加：

```bash
SUPPORT_API_TOKEN=your-actual-token-here
```

### 4. 启动服务并验证

```bash
curl http://localhost:18000/api/board/stats
# → {"issues_count": N, "source": "api", ...}
```

---

## 完整配置字段说明

```yaml
data_source:
  type: api
  api:
    base_url: "https://support.example.com"    # 必填：API 根地址

    # 认证方式（选一种）
    auth:
      type: bearer                              # bearer / basic / api_key / none
      token_env: "SUPPORT_API_TOKEN"            # 从环境变量读取 token（推荐）
      # token: "hardcoded-token"               # 直接写 token（不推荐，会入 git）

    # 接口路径
    endpoints:
      list: "/api/v2/tickets"                  # 必填：工单列表接口
      # detail: "/api/v2/tickets/{id}"         # 可选：单条工单详情

    # 字段映射（JSONPath 语法）
    field_map:
      external_id: "$.id"                      # 必填：唯一 ID
      summary:     "$.subject"                 # 必填：工单标题
      description: "$.description"             # 推荐：详细描述
      status:      "$.status"                  # 推荐
      assignee:    "$.assignee.name"           # 可选：支持嵌套路径
      reporter:    "$.requester.email"
      priority:    "$.priority"
      created:     "$.created_at"

    # 响应结构（列表在哪一层）
    response_path: "$.tickets"                 # 默认：根级数组 "$"

    # 分页（可选）
    pagination:
      type: page                               # page / cursor / offset
      page_param: "page"
      page_size_param: "per_page"
      page_size: 100
      max_pages: 50                            # 最多拉取 50 页
```

---

## 认证方式详解

### Bearer Token

```yaml
auth:
  type: bearer
  token_env: "MY_API_TOKEN"
```

请求头：`Authorization: Bearer <token>`

### Basic Auth

```yaml
auth:
  type: basic
  username_env: "API_USERNAME"
  password_env: "API_PASSWORD"
```

请求头：`Authorization: Basic <base64(user:pass)>`

### API Key（Header）

```yaml
auth:
  type: api_key
  header: "X-API-Key"
  key_env: "MY_API_KEY"
```

### API Key（Query 参数）

```yaml
auth:
  type: api_key
  param: "api_key"
  key_env: "MY_API_KEY"
```

---

## 真实接入示例

### Zendesk

```yaml
data_source:
  type: api
  api:
    base_url: "https://yourcompany.zendesk.com"
    auth:
      type: basic
      username_env: "ZENDESK_EMAIL"     # user@example.com/token
      password_env: "ZENDESK_API_TOKEN"
    endpoints:
      list: "/api/v2/tickets.json"
    field_map:
      external_id: "$.id"
      summary:     "$.subject"
      description: "$.description"
      status:      "$.status"
      assignee:    "$.assignee_id"
      created:     "$.created_at"
    response_path: "$.tickets"
    pagination:
      type: cursor
      next_page_path: "$.next_page"
```

### Freshdesk

```yaml
data_source:
  type: api
  api:
    base_url: "https://yourcompany.freshdesk.com"
    auth:
      type: basic
      username_env: "FRESHDESK_API_KEY"
      password_env: "X"               # Freshdesk 固定用 X 作为密码
    endpoints:
      list: "/api/v2/tickets"
    field_map:
      external_id: "$.id"
      summary:     "$.subject"
      description: "$.description_text"
      status:      "$.status"
      priority:    "$.priority"
      created:     "$.created_at"
    response_path: "$"                # 直接返回数组
    pagination:
      type: page
      page_param: "page"
      page_size_param: "per_page"
      page_size: 100
```

### Linear

```yaml
data_source:
  type: api
  api:
    base_url: "https://api.linear.app"
    auth:
      type: bearer
      token_env: "LINEAR_API_KEY"
    endpoints:
      list: "/graphql"              # Linear 使用 GraphQL，需额外配置 body
    field_map:
      external_id: "$.id"
      summary:     "$.title"
      description: "$.description"
      status:      "$.state.name"
      assignee:    "$.assignee.name"
      created:     "$.createdAt"
    response_path: "$.data.issues.nodes"
```

### 自研 REST API

```yaml
data_source:
  type: api
  api:
    base_url: "https://internal.mycompany.com/support"
    auth:
      type: api_key
      header: "X-Service-Token"
      key_env: "INTERNAL_API_TOKEN"
    endpoints:
      list: "/api/tickets/list"
    field_map:
      external_id: "$.ticket_id"
      summary:     "$.title"
      description: "$.content"
      status:      "$.state"
      assignee:    "$.handler"
      reporter:    "$.submitter.name"
      priority:    "$.urgency_level"
      created:     "$.submit_time"
    response_path: "$.data.list"
    pagination:
      type: offset
      offset_param: "offset"
      page_size_param: "limit"
      page_size: 50
```

---

## 调试技巧

### 1. 先用 curl 验证接口

```bash
curl -H "Authorization: Bearer $SUPPORT_API_TOKEN" \
  https://api.example.com/v2/tickets | python3 -m json.tool | head -50
```

### 2. 查看后端日志

```bash
tail -f APP/backend/logs/app.log | grep -i "provider\|api\|fetch"
```

### 3. 验证字段映射

后端加载后通过 API 查看一条工单详情：

```bash
TOKEN=<your-admin-token>
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:18000/api/issue/api:TICKET-001
```

---

## 接入 Checklist

- [ ] 目标 API 能通过 curl 访问，响应为有效 JSON
- [ ] 已识别工单列表的 JSON 路径（`response_path`）
- [ ] 已映射至少 `external_id` 和 `summary` 两个必填字段
- [ ] 认证凭据已写入 `.env`，未硬编码到 yaml
- [ ] 分页配置正确（`issues_count` 等于实际工单总数）
- [ ] 至少一条工单的 `description` 有内容（影响 AI 回复质量）
- [ ] 网络可达：后端所在机器能访问目标 API（注意代理、防火墙）
