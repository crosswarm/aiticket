# 演示数据管理

AITicket 内置 HR 工单演示数据集，帮助新用户在无需对接 Jira 的情况下快速体验系统的核心功能。

## 何时使用演示数据

- 初次部署，想先体验智能回复 / KB 检索 / 看板功能
- 培训 / 演示场景，不希望展示真实业务数据
- 开发调试，需要一组结构稳定的测试工单

演示数据与真实 Jira 数据是**互斥**的：安装演示数据会将数据源切换为 Excel 模式；清空后需要手动配置 Jira 连接。

---

## 安装演示数据（首次设置）

演示数据安装入口**仅出现一次**，在首次创建管理员账号之后：

1. 打开 `http://localhost:18000`
2. 系统检测到未创建管理员，显示初始化表单
3. 填写管理员用户名 + 密码，点击"创建管理员账号"
4. 弹出演示数据提示框（默认勾选"安装 HR 演示数据"）
5. 点击"继续"

> 如果选择**不安装**，后续不会再出现此提示。可在系统设置中手动配置数据源。

安装完成后系统自动切换到演示看板，16 条 HR 工单即时可用。知识库索引在后台运行（约 30 秒），完成后智能回复即可引用 KB 内容。

---

## 清空演示数据

演示数据安装后，可在**系统设置**中随时清空：

1. 点击右上角"设置"按钮
2. 滚动到"演示数据"分组
3. 点击"一键清空演示数据"
4. 在确认弹窗中点击"确认"

**清空操作包括：**

| 操作 | 说明 |
|------|------|
| 删除工单文件 | `data/imports/demo_hr_tickets.xlsx` |
| 删除知识库目录 | `KB/hr/`（含 4 个文档）|
| 回退配置文件 | `config/deployment.yaml` 恢复为示例配置 |
| 清理缓存 | 看板缓存 + HR 工单回复缓存 |
| 重置知识库索引 | 清空 chroma KB collection |

清空后系统进入**空数据状态**，配置 Jira 或其他数据源即可正式使用。

---

## 清空后如何配置真实数据源

- **Jira**：编辑 `APP/backend/config/deployment.yaml`，填写 `instance.name` 和 Jira 连接信息，重启服务。
- **Excel**：参见 [Excel 数据源接入手册](data-source-excel.md)
- **第三方 REST API**：参见 [第三方 API 数据源接入手册](data-source-api.md)

---

## 手动重新安装演示数据（CLI）

如果需要从命令行重新安装演示数据（例如开发调试或测试环境重置）：

```bash
python scripts/seed_demo_hr.py           # 生成文件（跳过已存在）
python scripts/seed_demo_hr.py --force   # 强制覆盖已有文件
```

生成后需要手动触发 KB 索引：

```bash
curl -X POST http://localhost:18000/api/kb/sync
```

---

## FAQ

**Q: 安装演示数据后，为什么智能回复不引用知识库？**

A: KB 索引是后台异步执行的，通常需要 15-60 秒。等待片刻后刷新页面，或手动触发 `POST /api/kb/sync`。

**Q: 清空后误删了演示数据，可以恢复吗？**

A: 可以。通过 CLI 重新运行 `python scripts/seed_demo_hr.py --force`，然后删除标记文件让安装入口重新出现（删除 `APP/backend/data/.demo_seeded`）。

**Q: Docker 部署时演示数据存放在哪里？**

A: 在容器内，演示数据存放在 `/data/imports/`（持久卷 `aiticket-data`）和 `/data/kb/hr/`，配置文件在 `/app/config/deployment.yaml`（绑定挂载，写入后同步回主机 `APP/backend/config/deployment.yaml`）。需要在 `.env` 中设置 `DATA_DIR=/data` 和 `KB_DIR=/data/kb`。
