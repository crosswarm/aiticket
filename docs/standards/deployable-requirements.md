# aiticket Deployable 版本要求规范

> 更新：2026-05-19  
> 适用范围：所有新功能开发，判断是否需要在 deployable 版本中支持

---

## 一、Deployable 是什么

`aiticket-deployable` 是 aiticket 的**对外交付版本**，面向工单处理团队的日常使用场景。

**定位**：为相关工单处理团队提供集中的前后端服务，服务本团队用户的 Web 端和 Skill 端使用。

**不是**：内部运营管理工具、需求研究平台、AI 训练平台。

技术上通过 `git cherry-pick` 从 `main` 同步，注入两个环境变量：
- `AITICKET_DEPLOYABLE=1` — 调度门控
- `AITICKET_ROLE=deployable` — strict mode（禁用默认账号 fallback）

---

## 二、功能边界

### 2.1 包含的功能（deployable 提供）

| 功能模块 | 说明 |
|---|---|
| 工单看板 | 工单查询、分析、状态管理、移动 |
| 智能回复 | 基于知识库的解决方案生成、回复弹窗 |
| 知识库查询 | KB 搜索、QA 问答 |
| 工单分类 | 自动标签、优先级、模块归属 |
| 月报生成 | 面向团队的月度工单分析报告 |
| KB 增量同步 | 知识库内容定期更新 |
| 夜间索引维护 | chroma 向量库重建/watchdog |
| Skill 端接口 | aiticket-suite / aiticket-reply 的全部 API 端点 |

### 2.2 不包含的功能（deployable 不提供）

| 排除模块 | 原因 |
|---|---|
| **需求池**（req-pool） | 内部产品研究，非工单处理范畴 |
| **需求规划**（Darwin / 需求分析 Agent） | 高级 AI 研究模块，依赖内部数据 |
| **Agent 调度管理界面** | Jobmaster UI / 调度配置属于运营管理 |
| **周报生成** | 内部汇报，`AITICKET_DEPLOYABLE=1` 自动跳过 |
| **竞品分析** | 内部战略工具 |
| **回复训练器管理** | 训练数据属于内部资产 |
| **内部 Fiona 问答** | Mini 专属功能 |

### 2.3 知识库资料包（单独部署）

deployable 版本的知识库需**独立安装资料包**，分两种：

| 类型 | 内容 | 安装方式 |
|---|---|---|
| **原厂资料包** | 标准产品文档、通用操作指南（跨客户通用） | 公开发布，`kb-setup --preset standard` |
| **私有化资料包** | 客户专属文档、定制流程、客开说明 | 客户自行维护，`kb-setup --preset private --source <path>` |

两种资料包可叠加使用。未安装资料包时，知识库查询返回空结果（不报错）。

---

## 三、新功能判断清单

开发新功能时，依次回答以下问题：

### Q1：功能面向谁？
- **工单处理团队日常使用** → 考虑 deployable
- **内部运营/AI 研究/需求分析** → 不进 deployable，在 main 内部使用

### Q2：功能是否依赖内部专有凭证？
- 依赖 Jira 管理员账号（qiangxiao）全局 fallback → **必须加 `is_strict_role()` 门控**，或设计为 per-user 凭证传入
- 依赖 PM 系统默认 token → **必须走 wallet 绑定**，strict mode 下无绑定则 401

### Q3：功能是否属于「调度/汇报」类？
- 周报类 → 加 `AITICKET_DEPLOYABLE == "1"` 跳过门控
- 月报/KB 同步/索引维护 → deployable 允许，无需门控

### Q4：功能数据是否含客户敏感信息？
- 含客户名称（LCZX/YYZJ/MC-REQ 等）、工单内容、员工数据 → **代码可进，结论/缓存文件不进 OSS-public**

### Q5：功能代码落在哪？
| 目录 | deployable | OSS-public |
|---|---|---|
| `APP/backend/` `APP/frontend/` | ✓ | ✓（无客户数据） |
| `deploy/scripts/` `scripts/` | ✓ | ✓ |
| `deploy/private/` | ✗ | ✗ |
| `.agent/skills/` | ✗ | ✗ |
| `design/` | ✗ | ✗ |

---

## 四、Strict Mode 行为要求

`AITICKET_ROLE=deployable` 触发 strict mode，**所有新功能必须遵守**：

1. Jira 调用必须携带显式 `session_cookies` 或 `username`，不能使用全局单例
2. PM 调用必须有 per-user wallet 绑定，无绑定 → `HTTP 401 PMNotBoundError`
3. 后台任务必须使用 `pick_jira_service_for_bg()` 从活跃用户池取会话
4. 违规 → `NoUserContextError` → `HTTP 401`，**不能静默降级**

实现检查：`from role_guard import is_strict_role, NoUserContextError`

---

## 五、快速判断口诀

```
面向工单处理团队 + 通用 API + per-user 凭证 + 无客户数据硬编码
→ 支持 deployable，无需特殊处理

依赖内部账号/管理报表/需求研究/Agent 调度管理
→ 不进 deployable，main 内部使用

功能产出含客户名称/业务数据
→ 代码可进，结论/缓存文件不进 OSS
```

---

## 六、相关文档

- [目录布局规范](directory-layout.md)
- [发布边界元数据](../../scripts/audit/check_layout.py)（`PUBLICATION_TAGS`）
- AGENTS.md § "Deployable Worktree 约定"
