# Jira 代理"绑定 + 校验 + 使用"铁论方案

> **铁论标记**：本文档描述的三层架构是不可随意修改的设计约束。
> 涉及端点：`/api/settings/jira-session-binding` / `/api/settings/jira-session-bind` / `/api/system/jira-session-status`
> 涉及文件：`main.py`、`jira_proxy.py`、`auth_service.py`

---

## 背景

Jira（jira.example.com）采用 MFA 认证，Basic Auth 被拦截，只能用浏览器 Cookie 中的 JSESSIONID。
历史上反复出现"绑定 403 被拒"的问题，根因是绑定、校验、使用三件事被混在一起，没有分层。

---

## 三层铁论（Save / Validate / Use）

```
┌────────────────────────────────────────────────────────────────┐
│  Layer 1: SAVE（绑定）                                          │
│  POST /api/settings/jira-session-binding                        │
│  ✅ 永远不联网，纯写 SQLite encrypted_token                      │
│  ✅ 任何主机（Mini/QCL/未来扩展）行为完全一致                     │
│  ✅ 禁止在本端点添加任何 Jira 探活逻辑                           │
├────────────────────────────────────────────────────────────────┤
│  Layer 2: VALIDATE（校验，best-effort，可选）                   │
│  POST /api/settings/jira-session-bind                           │
│  GET  /api/system/jira-session-status                           │
│  · 试探性调 jira /myself，获取 jira_name                      │
│  · mini 路径：proxies={"http": None, "https": None}（禁系统代理）│
│  · qcl 路径：proxies={MINI_PROXY_URL}（走 frp 隧道）            │
│  · 200  → verified=True / state="active"                        │
│  · 401  → 拒绝保存 / state="expired"（唯一判定过期的情况）       │
│  · 403  → verified=False,保存 / state="unverified"（IP 风控）   │
│  · 其他 → verified=False,保存 / state="unverified"              │
│  · 网络异常 → verified=False,保存 / state="unverified"          │
├────────────────────────────────────────────────────────────────┤
│  Layer 3: USE（业务请求实际使用 session）                        │
│  build_request_jira_client (main.py) per-user 创建 client        │
│  → JiraService(session_cookies={JSESSIONID, xsrf_token})        │
│  → base_url：mini=jira 直连 / qcl=MINI_PROXY_URL              │
│  → jira_proxy.py transparent_proxy 透传：                        │
│     · incoming JSESSIONID 优先（per-user 隔离）                  │
│     · 删除 Authorization header（防 Mini admin 串）              │
│     · 仅当请求方无 JSESSIONID 才回落 Mini 本地默认               │
└────────────────────────────────────────────────────────────────┘
```

---

## 五条不可违反的铁律

1. **Save 与 Validate 永远分离端点**
   - `/jira-session-binding`：禁止添加任何网络调用
   - `/jira-session-bind`：禁止把校验失败转为保存失败（除 401）

2. **校验路径必须显式禁用系统代理**
   - role=mini：`proxies={"http": None, "https": None}` — 强制项，不可依赖默认
   - macOS Surge/Clash 会拦截 HTTPS 请求，不禁则得到伪 403

3. **QCL 永远只是网络中转**
   - QCL 不持有任何 Jira session
   - 所有 session 在 SQLite users 表 per-user 加密存储
   - QCL 业务请求 + 校验请求均走 `MINI_PROXY_URL=http://127.0.0.1:5001`
   - Mini jira_proxy.py 不维护"全局 session bag"，只透传 per-request cookies

4. **403 ≠ session 无效**
   - 403 常见原因：IP 风控、CAPTCHA、UA 黑名单
   - 一律降级为 unverified 保存，让业务调用揭示真问题
   - 仅 401 才判定 session 过期

5. **session 不准跨用户共享**
   - `jira_proxy.py` `transparent_proxy` 中 `incoming.get('JSESSIONID')` 优先逻辑不可改
   - 用户带自己 JSESSIONID → 用用户的；不带 → 回落 Mini 默认
   - 回落时必须删除 Authorization header，防 Mini admin 串用

---

## 数据流

### Mini 用户绑定
```
Browser
  → POST /api/settings/jira-session-binding {jsessionid, xsrf, base_url}
  → SQLite users.encrypted_token (per-user AES 加密)
  ← {status: "success"}                              ← 无网络调用

[可选校验]
  → POST /api/settings/jira-session-bind
  → requests.get(jira/myself, proxies={None,None})
  → 200 → verified=True,jira_name
  → 403 → verified=False,reason="IP 风控"
  → 401 → HTTPException 401，不保存
```

### QCL 用户绑定
```
Browser → ticket.spux.cn → QCL nginx → QCL backend (18000)
  → POST /api/settings/jira-session-binding          ← 同 Mini，纯写 SQLite

[可选校验]
  → POST /api/settings/jira-session-bind
  → requests.get(jira/myself, proxies={MINI_PROXY_URL})
  → frp tunnel → Mini:5001 jira_proxy
  → Mini transparent_proxy 用 incoming JSESSIONID 出站
  → jira.example.com
  → 200/403/401 同 Mini 路径
```

### 任意主机用户业务请求
```
Browser → /api/board/fetch
  → build_request_jira_client (main.py)
  → 读 SQLite per-user JSESSIONID
  → JiraService(session_cookies={JSESSIONID:...},
                base_url=mini:None | qcl:MINI_PROXY_URL)
  → REST call

  [qcl 路径]
  → http://127.0.0.1:5001/rest/api/2/issue/...
  → frp → Mini jira_proxy.py transparent_proxy
  → 用 incoming JSESSIONID（用户自己的）出站 jira.example.com
  → response 透传
```

---

## 关键代码位置

| 模块 | 位置 | 说明 |
|------|------|------|
| Layer 1 Save | `main.py` `save_jira_session_binding` | 纯写 SQLite |
| Layer 2 Validate | `main.py` `jira_session_bind` | 试探性校验 + 保存 |
| Layer 2 Status | `main.py` `jira_session_status_endpoint` | 探活，30s 缓存 |
| Layer 3 Client | `main.py` `build_request_jira_client` | per-user JiraService |
| Layer 3 Transport | `jira_proxy.py` `transparent_proxy` (L671) | per-request 会话隔离 |
| 代理路由 | `main.py` `_resolve_jira_base_url` | mini直连/qcl走frp |
| Session 存储 | `auth_service.py` `upsert_jira_session_binding` | AES 加密存 SQLite |

---

## 不在本方案范围

- Jira session 自动刷新（`refresh_jira_session.sh` 独立模块）
- jira_proxy.py 响应缓存（`jira_cache_service.py` 独立模块）
- 多并发 Jira 实例支持（当前单实例 jira.example.com）
