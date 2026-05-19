# AITicket 可部署版 — 项目说明与功能清单

版本：feat/extract-deployable  
适用范围：各部门自主部署的独立实例（单租户多用户多项目）

---

## 一、定位与目标

AITicket 是一套面向产品/技术支持团队的 **AI 增强工单处理系统**，将 Jira 看板、知识库、智能回复和自我学习能力整合到一个轻量服务中。

**核心价值**：
- 处理工单时，AI 自动检索相关知识库并生成一键采纳的回复草稿
- 每次处理积累为知识，系统每晚自我学习，越用越准
- 知识库跨工单、文档、AI 综合三路来源，覆盖面广、可维护
- 网页和 Claude Code Skill 双客户端，适配不同工作习惯

**部署形态**：一部门一实例，独立服务器，`docker compose up` 30 分钟内上线。

---

## 二、完整功能清单

### 2.1 智能看板

| 功能 | 说明 |
|---|---|
| 多项目支持 | 支持同时配置多个 Jira 项目键，Nav 可切换当前项目 |
| 看板视图 | 按模块/状态展示未关闭工单，支持自定义分组规则 |
| 工单详情 | 展示描述、评论、附件、状态流转历史 |
| 工单搜索 | JQL 搜索 + 全文搜索，支持 project_key 过滤 |
| 模块分类 | 按 `deployment.yaml` 中 `module_taxonomy` 自动归类工单 |
| 看板同步 | 手动或定时从 Jira 拉取最新数据到本地向量索引 |
| Jira cookie 绑定 | 用户在 web 提交 Jira session cookie，系统代替用户调 Jira API |
| 历史工单 lazy 索引 | 切换到尚未建索引的项目时，后台异步拉取历史工单写入 Chroma，前端顶栏显示进度 |

#### 首次切换项目的索引行为

切换到尚未建索引的项目时：
- **看板**：立即从 Jira 实时拉取，无延迟
- **相似工单召回**：后台异步建索引，顶栏显示「索引中 N%」
- 索引完成后，AI 回复自动启用历史经验加持（相似工单 + KB 双路召回）

首部署建议先跑一次全量种子，避免用户等待：

```bash
docker compose exec aiticket python -m scripts.seed_projects --days 180
```

### 2.2 智能回复

| 功能 | 说明 |
|---|---|
| 一键生成回复 | 读取工单上下文 + 知识库证据，生成结构化回复草稿 |
| 多 LLM 支持 | 支持智谱、MiniMax、OpenAI 兼容接口，可按功能配置优先链 |
| 回复风格规则 | 管理员维护 `reply_style_rules.md`，AI 回复遵守团队风格约束 |
| 产品事实注入 | `product_facts.md` 中的约束事实自动注入回复生成上下文 |
| 回复反馈记录 | 用户采纳/修改/拒绝都记录到 `feedback_log.jsonl`，作为训练输入 |
| 解决方案预加载 | 弹窗打开即显示解决方案（非阻塞预生成，0 感知延迟） |
| 多轮精修 | 支持基于草稿继续追问和修改 |

### 2.3 自学习与自训练（已包含）

这是系统持续提升回复质量的核心机制，全自动运行，无需人工干预。

| 机制 | 触发时机 | 效果 |
|---|---|---|
| **回复反馈收集** | 每次用户采纳/修改/拒绝回复 | 积累高质量样本，识别哪类回复被修改最多 |
| **夜间训练** (`nightly-training.json`) | 每晚 02:00 | 分析反馈日志，更新 `reply_style_rules.md`（语气/格式/禁用词等规则自动优化） |
| **KB 自动采集** (`KBAutoImport`) | 每次生成回复时实时触发 | 识别回复中的产品知识（步骤/支持边界/解决方案），自动入库为 `user_contributed` 条目 |
| **已采纳回复提取** (`weekly-adopted-extract.json`) | 每周日 03:30 | 从被采纳的回复中提炼 operational fact，追加到 KB |
| **工单模式学习** (`weekly-pattern-learning.json`) | 每周日 04:00 | 分析高频工单模式，识别哪些模块的问题反复出现，更新推荐内容 |
| **历史交接记录提取** (`weekly-handover-extract.json`) | 每周日 04:30 | 从历史交接工单中提取业务知识入库 |
| **KB 事实定期提炼** (`weekly-fact-extraction.json`) | 每周日 03:00 | 从 AI 编译条目中蒸馏产品约束事实到 `product_facts.md` |

**自学习数据流：**
```
用户处理工单
    ↓
采纳/修改回复 → feedback_log.jsonl
    ↓                    ↓
KBAutoImport         夜间训练任务
(实时入库              (每晚更新
user_contributed)     reply_style_rules)
    ↓                    ↓
  KB 丰富          回复质量提升
    ↓
下次搜索命中更好的证据
    ↓
回复草稿更准确
```

### 2.4 知识库（KB）

| 功能 | 说明 |
|---|---|
| 多源融合 | `doc`（文件）/ `kb_compiled`（AI综合）/ `user_contributed`（自动采集）/ `ticket_case`（工单案例）/ `fact`（产品事实）五路来源 |
| 混合搜索 | BM25 全文搜索（FTS5）+ 向量搜索（ChromaDB），按 55:25:20 混合排分 |
| 话题编译 | 按 `topic.md` 中定义的话题树，AI 跨文档综合生成结构化 KB 条目 |
| 多项目过滤 | 搜索时按 `project_key` 过滤，同时返回全局（`_global`）内容 |
| PRD 辅助 | 基于 KB 证据生成 PRD 初稿 + 章节覆盖检查 |
| KB Q&A | 自然语言提问，AI 从 KB 检索并综合回答 |
| 健康检查 | 覆盖率报告 + 缺失话题检测（`/api/kb/lint`） |
| 自动保护 | sync 前备份、sync 后验证，防止 AI 编译条目被误清 |

### 2.5 定时任务（JobMaster）

| 任务文件 | 时间 | 说明 |
|---|---|---|
| `daily-summary-0725.json` | 每天 07:25 | 生成日报（看板状态摘要） |
| `nightly-training.json` | 每晚 02:00 | 回复训练 + 规则更新 |
| `weekly-*.json` | 每周日 03:00-05:00 | KB 知识提炼 + 模式学习 |
| `jobmaster-heartbeat.json` | 每 5 分钟 | 任务调度器健康检查 |
| `agent-memory-audit.json` | 定期 | Agent 内存审计 |

### 2.6 用户与认证

| 功能 | 说明 |
|---|---|
| 多用户 | 每用户独立账号，cookie session 隔离 |
| 管理员角色 | admin 用户可管理其他用户、查看系统状态 |
| 项目偏好 | 每用户保存当前项目设置，跨设备同步 |
| Bootstrap 建账 | `python -m bootstrap.seed_admin` 创建首个管理员 |

### 2.7 客户端

**网页客户端**
- `board.html` — 智能看板主页面
- `kb.html` — 知识库搜索/编辑页面
- Nav 展示实例名称 + 项目切换器
- 自适应不同分辨率

**Claude Code Skill 客户端**（`client-skill/aiticket/`）

| Skill 命令 | 功能 |
|---|---|
| `/aiticket-login` | 首次绑定 API_BASE + 获取 Bearer token |
| `/aiticket-search <jql>` | 搜索工单列表 |
| `/aiticket-reply <issue-key>` | 生成智能回复草稿 |
| `/aiticket-kb <query>` | 查询知识库 |
| `/aiticket-switch-project <key>` | 切换当前项目 |

安装：`cp -r client-skill/aiticket ~/.claude/skills/aiticket`，填写 `config.json`。

---

## 三、技术栈

| 层 | 技术 |
|---|---|
| API 服务 | Python 3.11 + FastAPI + uvicorn（单 worker） |
| 向量搜索 | ChromaDB 1.5+ + sentence-transformers 本地 embedding |
| 全文搜索 | SQLite FTS5（BM25） |
| 用户数据 | SQLite（auth.db / jobmaster.db） |
| 前端 | 原生 HTML + JS（无前端框架），单文件部署友好 |
| LLM | 智谱 GLM-4 / MiniMax / 任意 OpenAI 兼容接口 |
| 任务调度 | 内置 JobMaster（SQLite 持久化 + HTTP API） |
| 容器化 | Docker Compose（python:3.11-slim 基础镜像） |

---

## 四、部署架构

```
┌─────────────────────────────────────────────────────┐
│                  部门服务器                          │
│                                                     │
│  ┌──────────────────┐    ┌─────────────────────┐   │
│  │   nginx           │    │  aiticket-api       │   │
│  │   :80 / :443      │───▶│  :18000 (uvicorn)  │   │
│  │   静态文件 + 代理  │    │  FastAPI + JobMaster│   │
│  └──────────────────┘    └────────┬────────────┘   │
│                                   │                 │
│                           data/   │                 │
│                           ├── kb/            (挂载) │
│                           ├── sqlite/        (挂载) │
│                           ├── chroma_kb/     (挂载) │
│                           └── notes/topic.md (挂载) │
└─────────────────────────────────────────────────────┘

用户访问 → nginx → API → Jira（部门内网）
                       → LLM API（云服务）
```

**三步启动（Docker）：**
```bash
git clone <repo> && cd aiticket-deployable
cp APP/backend/config/deployment.yaml.example APP/backend/config/deployment.yaml
# 编辑 deployment.yaml 填写 Jira URL + 项目键
cp .env.example .env
# 编辑 .env 填写 LLM API Key
docker compose up -d
docker compose exec aiticket-api python -m bootstrap.seed_admin
# → 打开浏览器 http://服务器IP
```

---

## 五、配置文件速查

| 文件 | 作用 | 是否进版本库 |
|---|---|---|
| `.env` | LLM API Key、端口、DATA_DIR | 否（.gitignore） |
| `APP/backend/config/deployment.yaml` | 实例身份、Jira URL、项目键、模块分类 | 否（.gitignore） |
| `APP/backend/data/reply_style_rules.md` | 回复风格约束（AI 自动更新+人工可编辑） | 是 |
| `APP/backend/data/product_facts.md` | 产品约束事实（AI 自动提炼+人工可编辑） | 是 |
| `data/kb/` | 知识库源文档目录（运行时 volume） | 否 |
| `data/notes/topic.md` | KB 话题树配置 | 否 |

---

## 六、功能边界（不包含）

- **不支持跨实例数据共享**：各部门的 KB 和回复数据相互独立
- **不支持 Jira 之外的工单源**（GitHub Issues 等）
- **不支持 SaaS 多租户**（需要一部门一实例，不共享数据库）
- **无内置备份策略**：建议挂载 `data/` volume 到有定期快照的存储
- **飞书/PM 系统集成**：默认关闭，需在 `deployment.yaml` 开启后单独配置

---

## 七、交付物清单

```
aiticket-deployable/
├── README.md                         快速开始（10 行）
├── docker-compose.yml                一键启动
├── Dockerfile
├── .env.example                      环境变量模板
├── APP/
│   ├── backend/
│   │   ├── config/
│   │   │   ├── deployment.yaml.example   实例配置模板
│   │   │   └── loader.py                 配置加载器
│   │   └── bootstrap/
│   │       ├── init_db.py                数据库初始化（幂等）
│   │       ├── seed_admin.py             创建首个管理员
│   │       └── verify_health.sh          健康验证脚本
│   └── frontend/
│       └── *.html + assets/              网页客户端
├── client-skill/
│   └── aiticket/                     Claude Code Skill 客户端
├── deploy/
│   ├── docker/nginx.conf.example     nginx 反代配置
│   └── native/
│       ├── install.sh                原生安装脚本
│       └── aiticket.service          systemd 单元
└── docs/
    ├── project-overview.md           本文档
    └── kb-maintenance-guide.md       知识库维护手册
```
