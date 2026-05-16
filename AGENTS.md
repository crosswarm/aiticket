# AITicket 开发规范

## 🤖 智能体通用行为准则

> 本节适用于系统中所有 Agent（内部 worker / master / dever / 外部桥接）。

### 原则零：主动智能（最高优先级）

**主动思考、主动发现、主动解决问题。**

| 维度 | 被动（不允许） | 主动（要求） |
|------|--------------|------------|
| 问题发现 | 等人工指出异常再处理 | 持续监控输出质量，发现偏差主动介入 |
| 问题处理 | 发告警等人工响应 | 在权限范围内直接修复，事后报告结果 |
| 知识缺口 | 遇到不知道的就说"不知道" | 识别盲区，主动触发 KB 补充 |
| 任务质量 | 完成交代的步骤就算交差 | 完成后自我审查：是否达到目标？ |

**判定标准**：一个合格 Agent 的工作结果，应该让用户感到"它比我更早意识到这个问题，已经处理好了"。

---

## 📋 核心规则

1. **先思考再动手**。明确陈述假设，不确定就问而不是猜；当存在歧义时呈现多种解读；如果有更简单的方案，大胆说出来。

2. **简洁优先**。用最少的代码解决问题。不加没被要求的功能，不为只用一次的代码建抽象，不为不可能发生的场景写错误处理。

3. **外科手术式修改**。只动必须动的地方，只清理自己制造的混乱。编辑现有代码时，不要顺手改进相邻代码，不要重构没坏的东西。

4. **目标驱动执行**。定义成功标准，循环验证直到达成。

---

## 🛠 技术规范

### Python 环境

```bash
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
.venv\Scripts\activate             # Windows
pip install -r APP/backend/requirements.txt
```

### 代码风格

```bash
ruff check APP/backend/
ruff format APP/backend/
```

### 运行测试

```bash
pytest APP/backend/tests/ -v
```

---

## 📁 模块文件映射

| 模块 | 后端服务 | 前端页面 |
|------|----------|----------|
| 智能看板 | `board_service.py`, `board_service_chroma.py` | `board.html` |
| 智能报告 | `weekly_analysis.py`, `monthly_analysis.py` | `report.html` |
| 需求规划 | `requirements_pool_service.py` | `requirements.html` |
| 知识库 | `kb_runtime_service.py`, `kb_compile_service.py` | `kb.html` |
| JobMaster | `APP/backend/services/job_master.py` | — |

---

## ⛔ 交付标准

**Agent 不得让用户当测试员**。任何"已完成/已修复"的声明，**必须**在 Agent 侧完成端到端验证后才能说出。

**E2E 验证必经步骤**：
1. **成功路径**：真实调一次目标 API，响应符合预期
2. **字段验证**：响应 JSON 结构与前端期望一致
3. **错误路径**：至少 1 个异常样例，前端能明确感知
4. **回归检查**：修复 A 功能后，B/C 已有功能仍可用

---

## 🔄 Ralph Loop 功能开发规范

当任务需要实现一个或多个可验证的功能点时，走 Ralph 流程：

```
1. 建故事文件   docs/user-stories/<模块>-<功能>.json
2. 启动循环     bun run ralph
3. 循环结束条件 所有故事 passes: true
4. 验收证据     scripts/ralph/log.md + JSON 故事文件
```

1. **看门狗检查**：每个定时任务必须有对应的看门狗（watchdog），定期检查任务是否正常运行、产出物是否生成
2. **AI 自动诊断**：看门狗发现异常时，**第一时间通知 Claude 或降级 LLM 来处理**，而非直接通知用户
3. **用户通知**：AI 诊断+修复完成后，只通知用户最终结果（修复成功/需人工介入），**避免消息轰炸**

#### 实现要求

| 要素 | 要求 | 说明 |
|------|------|------|
| **退出码检查** | 每次执行记录退出码到日志 | `echo "EXIT=$?" >> $LOG` |
| **产出物校验** | 检查预期文件是否生成、大小是否 >0 | 空文件 = 失败 |
| **超时保护** | 设置合理超时，超时即判定失败 | `timeout 300 command` |
| **重试策略** | 失败后自动重试 1 次（间隔 60s） | 重试仍失败才升级 |
| **AI 诊断** | 读取错误日志 → 分析根因 → 尝试自动修复 | 用 Claude scheduled trigger 或本地脚本调用 LLM |
| **降级通知** | AI 修复失败时飞书通知用户，附上诊断结论 | 消息格式：`[任务名] 失败 → AI诊断: {原因} → 需人工: {建议}` |
| **成功静默** | 正常执行成功时**不通知**用户 | 只有异常才通知 |

#### 通知策略（防消息轰炸）

- **正常执行**：静默，仅写日志
- **自动修复成功**：静默或低优先级通知（每日汇总）
- **自动修复失败**：立即通知用户，附诊断报告
- **连续失败 ≥3 次**：升级通知（高优先级）

#### 当前定时任务清单

| LaunchAgent | 功能 | 频率 | 看门狗 |
|-------------|------|------|--------|
| `com.aiticket.weekly-report` | 周报生成 | 周日 9:00 | report-watchdog |
| `com.aiticket.monthly-report` | 月报生成 | 每月 1 号 | report-watchdog |
| `com.aiticket.watchdog` | 后端保活 | 每 120s | 自身 |
| `com.aiticket.evolution` | 分类规则进化 | 每日 | 需补充 |
| `com.aiticket.reply-trainer` | 回复训练器同步 | 每日 | 需补充 |
| `com.aiticket.trainer-sync` | 训练数据同步 | 每日 | 需补充 |

### Agent 自监督契约（2026-05-16 起强制）

凡是有 schedule 触发的 agent，必须满足以下全部条件，否则 pre-commit `audit_schedule_registry.py --strict` 拒绝：

1. **schedule JSON 含 `agent_hint` 字段**，指向该 agent 的 name（用于 SCHEDULE_AGENT_MAP 自动派生 + agent_audit 检查）
2. **继承 `AgentSelfMonitorMixin`**（`APP/backend/agents/self_monitor_mixin.py`），类属性声明 `expected_run_interval_hours`（adhoc/无 schedule 的 agent 设 None）
3. **`health_check()` 调用 `self_check_last_run()`** 并将结果纳入 healthy 判定

`jobmaster-agent-audit` schedule 每 10 分钟扫描一次所有含 `agent_hint` 的 schedule，若 `2 × cadence` 内无 succeeded 行则发飞书告警。

#### Schedule-driven agent 对照表

| schedule_id | agent_hint | cadence | expected_run_interval_hours |
|---|---|---|---|
| nightly-exploration | competitor | 24h | 24 |
| darwin-reqpool-eval | darwin | 24h | 24 |
| weekly-fact-extraction | kb_fact | 7d | 168 |
| weekly-adopted-extract | adopted | 7d | 168 |
| weekly-report | req_analyst | 7d | 168 |
| weekly-req-analysis | req_cluster | 7d | 168 |
| daily-reqpool-ingest | req_analyst | 24h | 24 |
| nightly-training | reply | 24h | 24 |
| （更多由 Ralph Loop A1/B1 补全） | | | |

---

## 📂 目录约定：`_local/` vs git-tracked

新建文件时先问：**QCL 用户页面需要读它吗？**

| 答案 | 放哪里 |
|---|---|
| 是 | 原目录（正常 git 追踪，随 `git push deploy` 上 QCL） |
| 否 | 最近的 `_local/` 子目录（gitignored，只在 Mini 上） |

**已建立的 `_local/` 目录**（`.gitignore` 中的 `**/_local/` 自动排除）：

```
_local/                           顶层开发笔记、DESK 书籍、design 文档
conclusion/_local/                离线分析产物（lczx、bip-workflow、Topics 等）
APP/backend/_local/               evolution_core（Phase 3 迁移后）
APP/backend/scripts/_local/       dev-only 脚本（Phase 3 迁移后）
APP/backend/data/_local/          reply_trainer 等离线数据
```

**共享数据（双向同步 Mini ↔ QCL）**：
- `conclusion/MonthlyReports/`, `conclusion/WeeklyReports/`, `conclusion/requirements/`
- 同步脚本：`APP/deploy_scripts/sync_shared_data.sh --push/--pull/--dry-run`

---

## 📐 规范文档（Spec）位置

跨模块、长期权威的规范文档（命名 `MC-{域}-V{版本}.md`，例如 `MC-AGENT-MEMORY-V1.0.md`）按下面分类：

| 类型 | 位置 | git-tracked | 说明 |
|------|------|-------------|------|
| QCL 运行时需要参照的协议/接口规范 | `design/spec/` | 是 | QCL 后端代码读取，随 `git push deploy` 上传 |
| 内部架构/记忆体系/治理规范（仅开发者参考） | `_local/design/specs/` | 否 | 与 `_local/design/plan.md` 同等级，gitignored |
| 单期需求规划/PRD/任务计划 | `_local/design/plans/`（已有） | 否 | 命名格式 `YYYYMMDD-{模块}-{主题}.md` |

**判定**：QCL 用户页面或后端运行时是否需要读它？ 是 → `design/spec/`；否 → `_local/design/specs/`。

> 历史遗留：`design/spec/` 下已有 `MC-AGENT-ORCHESTRATOR-V1.0.md`、`MC-REQ-LCZX-CR*` 等，已 git-tracked 的不强制迁；新增文档按上表分流。

---

## 🤖 Agents 平台统一注册表

### 角色种类（`kind` 字段）

| kind | 含义 | 示例 |
|------|------|------|
| `internal_master` | 常驻主导 Agent，有 subagent 授权能力 | UXMaster, PRDMaster, ClaudeAgent |
| `internal_dever` | 常驻执行 Agent，无授权能力 | UXDever, PRDDever |
| `internal_worker` | 内部工作者（旧 agents 默认） | DarwinAgent, ReplyAgent … |
| `omc_subagent` | OMC 外部 subagent，必须由父 agent 授权 | omc_designer, omc_planner … |
| `external` | 第三方桥接（预留） | — |

### 父-子 OMC 授权映射

| OMC subagent | 父 Agent |
|---|---|
| designer | ux_master |
| planner, analyst, critic, verifier, refactor_planner | prd_master |
| 其余 14 个（executor/debugger/code-reviewer 等） | claude |

映射依据 frontmatter 名称/描述关键词，可在对应 `agents/identity/omc_*.yaml` 内手动覆盖 `parent_agent` 字段。

### 生命周期握手（4 步）

```
agents.html 用户 → POST /api/agents/omc_designer/trigger
  → 后端检测 kind=omc_subagent → 路由到父 agent
  → UXMaster.authorize_subagent(child=omc_designer, payload)
     ├ 校验归属 + health + 配额
     ├ 通过 → 写 agent_tasks + JobMaster authorize_claude_task
     └ 驳回 → 返回 {status:"rejected", reason:"..."}
  → Claude Code 端拾取 → 执行
  → UXMaster.on_subagent_finished(task_id, result)
```

### `triggerable_via` 限制

- `direct`：可从 agents.html / API 直接 POST trigger（默认）
- `parent_only`：OMC subagent 强制，不可绕过父 agent（自动修正）
- `schedule_only`：仅定时任务可触发

### identity yaml 必填字段（omc_subagent）

```yaml
kind: omc_subagent
parent_agent: ux_master        # 必填
omc_subagent_type: oh-my-claudecode:designer  # 必填，透传给 Claude Code
source_md: ~/.claude/plugins/cache/.../agents/designer.md
triggerable_via: parent_only   # 自动修正，可不填
```

### 新增 OMC subagent 流程

1. OMC 插件目录有新 `.md` → 重启后端 → `omc_bridge` 自动扫描
2. 自动落盘 `agents/identity/omc_<name>.yaml`（已存在则跳过）
3. 自动映射父 agent + 注册为固定角色
4. 如需覆盖父 agent：手动编辑落盘 yaml 的 `parent_agent` 字段 + 重启

### action_required 数据契约

`GET /api/agents/jobmaster` 返回字段 `action_required: []`，聚合多源：

```jsonc
{
  "kind": "theme_confirm",           // theme_confirm | human_review | jobmaster_decision
  "title": "需求池流水线等待主题确认",
  "description": "已聚 13 个主题，请确认后进入加工流程",
  "parent_id": "pipeline_xxx",
  "since": "2026-04-30T07:50:00Z",
  "actions": [
    {"id":"confirm","label":"确认主题","method":"POST","path":"/api/requirements_pool/themes/confirm"},
    {"id":"drop",   "label":"全部丢弃","method":"POST","path":"/api/requirements_pool/themes/drop"}
  ]
}
```

### 关键文件索引

| 路径 | 说明 |
|------|------|
| `APP/backend/agents/registry.py` | AgentRegistry 单例，`build_agent_summary` 输出 `kind/parent_agent/sub_agents` |
| `APP/backend/agents/identity/_schema.yaml` | identity yaml 全字段说明 |
| `APP/backend/agents/identity_schema.py` | Pydantic 校验模型（AgentIdentity） |
| `APP/backend/agents/parent_mixin.py` | ParentAgentMixin 授权协议 |
| `APP/backend/agents/omc_bridge.py` | OMC 扫描 + 映射 + 注册桥接器 |
| `APP/backend/api/agents_router.py` | `/api/agents` + trigger + jobmaster（含 action_required） |

---

## 🔑 PM 多用户会话绑定

PM 系统 session 绑定登录出口 IP，需通过代理路由。架构与操作详见 memory：
- `project_pm_session_proxy_fix.md` — 跨主机 session 代理原理
- `project_pm_multiuser_connect_proxy.md` — CONNECT 代理 + SSH 隧道 + nginx 架构
- `project_pm_peruser_proxy_routing.md` — per-user proxy 路由与 wallet 机制

---

## 🤖 Agent 责任分工与对抗审核（2026-05-02）

### 需求池责任链

| 角色 | Agent Name | Nickname | 职责 |
|------|-----------|---------|------|
| 需求池主负责 | `req_analyst` | Atlas (PRDever) | 聚类 → 充实 → 收集拆解调研 → 产品信号提炼（Pipeline Stage 3） |
| 需求规划主责 | `prd_master` | Victor (PRDMaster) | 候选方案 → PRD文档 → 对 Atlas 产出对抗审核（Pipeline Stage 4 + 规划） |

**Atlas 的完整职责**：需求池全链路（聚类/充实/分析/拆解），收集工单功能诉求，识别跨工单产品缺口。所有产出交付 Victor 审核前不视为完成。

**Victor 的对抗审核四维标准**：
1. 用户场景是否真实存在（有工单原话佐证）
2. 支撑工单数是否充分（至少 2 条）
3. 根因分析是否成立（逻辑链完整）
4. 优先级排序是否合理（影响面 × 成本 × 战略）

Victor 对每份 Atlas 产出必须主动提出合理质疑，不照单全收。

### UX 责任

| 角色 | Agent Name | Nickname | 职责 |
|------|-----------|---------|------|
| UX 主导 | `ux_dever` | Muse | UX 洞察提炼 + 交互改善方案 + 组件规范（已吸收 Aria/ux_master 全部职责） |

### Agent 合并历史（2026-05-02）

| 已归档 | 合并去向 | 历史任务 | 归档路径 |
|--------|---------|---------|---------|
| `prd_dever` (Rex) | → `req_analyst` (Atlas) | 0 条 | `identity/_archived/prd_dever_20260502.yaml` |
| `req_solution` (Sage) | → `prd_master` (Victor) | 21 条 | `identity/_archived/req_solution_20260502.yaml` |
| `ux_master` (Aria) | → `ux_dever` (Muse) | 6 条 | `identity/_archived/ux_master_20260502.yaml` |

历史 `agent_tasks` 记录不变（`agent_name` 列保持原值）；registry 通过 `aliases` 字段聚合展示到合并后 agent 的卡片。

---

## Local LLM 生命周期强制规则

**规则**：任何调用 SuperGemma4（本地 MLX）的代码路径，必须遵循「探活→启动→执行→关灯」四步流程。

### 标准模式

```python
from services.local_llm_lifecycle import lifecycle

with lifecycle("task_name", required=False) as provider:
    # provider = "local" 或 "zhipu"（local 不可用时的降级）
    run_logic(provider=provider)
# finally 自动调用 shutdown_if_started_by_us
```

### 禁止模式

```python
# ❌ 只探活不关灯
if ensure_running():
    run_logic()
# 缺少 shutdown_if_started_by_us → GPU 永远不释放

# ❌ shutdown 不在 finally 里
if ensure_running():
    try:
        run_logic()
    except Exception:
        ...
shutdown_if_started_by_us("task")  # 异常路径会跳过这里
```

### Schedule JSON 规范

任何 `task_type: script` 且脚本调用 Local LLM 的调度，必须声明：

用户故事格式：
```json
[{
  "category": "functional",
  "description": "用一句话说明功能",
  "steps": ["可验证的步骤1", "步骤2"],
  "passes": false
}]
```

---

## 🌐 多平台兼容

本项目支持在 macOS / Linux / Windows 三端运行，编写脚本时注意：
- 路径分隔符使用 `os.path.join()` 或 `pathlib.Path`
- 不硬编码 `~/.xxx` 系统路径，用 `DATA_DIR` 环境变量
- 启动脚本同时提供 `.sh`（Unix）和 `.ps1`（Windows）版本

---

## 📖 开发前必读

| 文档 | 路径 |
|------|------|
| 项目总纲要 | `README.md` |
| 部署手册 | `DEPLOYMENT.md` |
| 架构概览 | `docs/project-overview.md` |
| KB 维护指南 | `docs/kb-maintenance-guide.md` |
| 贡献指南 | `CONTRIBUTING.md` |
