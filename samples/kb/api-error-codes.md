# API 错误码说明

## 通用错误

| 错误码 | 含义 | 处理建议 |
|--------|------|----------|
| 401 | 未登录或 Token 过期 | 重新登录，检查 Cookie/Token |
| 403 | 无权限 | 检查账号角色和权限配置 |
| 404 | 资源不存在 | 确认资源 ID 和 URL |
| 429 | 请求过于频繁 | 降低调用频率，实现退避重试 |
| 500 | 服务器内部错误 | 查看后端日志，联系管理员 |

## Jira 连接错误

| 场景 | 提示 | 解决方法 |
|------|------|----------|
| SSL 证书错误 | `SSL verification failed` | 设置 `JIRA_SSL_VERIFY=false` 或配置 CA Bundle |
| 认证失败 | `401 from Jira` | 检查 Jira session，重新在设置页登录 |
| 连接超时 | `Connection timeout` | 检查网络连通性，确认 `JIRA_BASE_URL` 正确 |

## LLM API 错误

| Provider | 常见错误 | 处理 |
|----------|----------|------|
| 所有 | `API key invalid` | 检查 `.env` 中的 API Key |
| 所有 | `Rate limit exceeded` | 配置多个 Provider 作为 fallback |
| OpenAI | `Quota exceeded` | 检查账单，升级套餐 |
