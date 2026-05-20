# AITicket 操作手册索引

## 管理员

| 文档 | 内容 |
|------|------|
| [演示数据管理](demo-data-management.md) | 安装 HR 演示数据、系统设置里一键清空、CLI 重置 |
| [Excel / CSV 数据源接入](data-source-excel.md) | 从 Excel 文件导入工单，column_map 完整字段说明 |
| [第三方 REST API 数据源接入](data-source-api.md) | Zendesk、Freshdesk、Linear、自研 API 接入配置 |
| [知识库上传与解析](kb-upload-guide.md) | 支持格式、上传方式、索引原理、检索调试 |

## 用户

| 文档 | 内容 |
|------|------|
| [Claude Code Skill 使用手册](client-skill-guide.md) | 安装、配置 Token、斜杠命令、多服务器场景、故障排查 |

## 快速链接

- 部署：根目录 `README.md` → "快速开始"章节
- Docker 启动：`docker compose up -d`
- 本地开发：`cd APP/backend && uvicorn main:app --reload --port 18000`
- 首次访问：`http://localhost:18000` → 创建管理员 → 选择是否安装演示数据
