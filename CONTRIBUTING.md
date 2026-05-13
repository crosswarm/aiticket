# 贡献指南

感谢你对 AITicket 的关注！我们欢迎所有形式的贡献。

## 快速入门

```bash
git clone https://github.com/iuap/aiticket.git
cd aiticket
cp .env.example .env          # 填写 JIRA_BASE_URL 和 LLM API Key
docker compose up             # 启动服务
open http://localhost:18000
```

## 贡献流程

1. **Fork** 本仓库
2. 新建功能分支：`git checkout -b feat/your-feature`
3. 编写代码 + 测试
4. 确保测试通过：`pytest APP/backend/tests/`
5. 提交 PR，标题格式：`feat(模块): 简短描述`

## PR 规范

- 每个 PR 专注一件事
- 添加对应测试（覆盖新增的 API 或业务逻辑）
- 更新 `DEPLOYMENT.md`（如涉及新的环境变量或部署步骤变更）

## 代码规范

```bash
# Python：使用 ruff 格式化
ruff check APP/backend/
ruff format APP/backend/
```

## Issue 提交

使用 GitHub Issues 报告 Bug 或提功能需求，请填写对应模板。

## 许可证

本项目采用 [GNU AGPL v3.0](LICENSE) 许可证。提交 PR 即表示你同意以该许可证发布你的代码。
