# AITicket 开发规范

## 跨项目仓库血缘与发布规范（强制）

执行跨仓移植、分支同步、Mini/172 发布、认证数据库或密钥变更、用户批量变更、发布验收前，所有 Agent 必须先完整读取 `docs/standards/repository-lineage-and-release.md`。该文件是权威规范，并与 `/Users/cfone/Studio/aiticket/docs/standards/repository-lineage-and-release.md` 保持内容一致。deployable 的权威仓库是 `https://github.com/crosswarm/aiticket`；QCL 已废弃，旧文件中相冲突的 QCL 指令不再有效。

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
| deployable 版本要求 | `docs/standards/deployable-requirements.md` |
