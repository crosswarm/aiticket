<div align="center">

# AITicket

**智能 Jira 工单分析 · 看板 · 周月报系统**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![CI](https://github.com/crosswarm/aiticket/actions/workflows/ci.yml/badge.svg)](https://github.com/crosswarm/aiticket/actions/workflows/ci.yml)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue)](https://github.com/crosswarm/aiticket/pkgs/container/aiticket)

</div>

---

## 它能做什么

| 模块 | 功能 |
|------|------|
| **智能看板** | 工单自动分类 · AI 回复建议 · 置信度评分 · 一键看板 |
| **智能分析** | 按主题/优先级/模块聚合 · 异常工单检测 · 趋势对比 |
| **周月报** | 定时生成 Markdown 报告 · 推送飞书/钉钉/WeCom |
| **KB 知识库** | 把自家文档/FAQ 向量化 · 语义检索 · 辅助 AI 回复 |

## 5 分钟快速上手

```bash
# 1. 克隆 & 配置
git clone https://github.com/crosswarm/aiticket.git && cd aiticket
cp .env.example .env
# 编辑 .env：填写 JIRA_BASE_URL 和至少一个 LLM API Key

# 2. 启动
docker compose up

# 3. 打开
open http://localhost:18000
# 默认账号：admin / admin（首次登录请立即修改密码）
```

> **最低要求**：Docker 20.10+、4 GB 内存、10 GB 磁盘

## 接入自己的 Jira 和 KB

```bash
# 配置 Jira
JIRA_BASE_URL=https://jira.mycompany.com   # 在 .env 填写

# 导入知识库（把你的 Markdown 文档放入 KB/ 目录）
cp -r /your/docs/dir KB/
docker compose exec backend python scripts/import_kb.py

# 建立历史工单索引（首次必跑，使智能回复能检索相似历史案例）
# 约 5-30 分钟，视 Jira 工单量和网络延迟；之后每天自动增量
docker compose exec aiticket python -m scripts.seed_projects --days 180

# 配置主题树（可选，让分类更贴合你的业务）
cp samples/topic.example.md APP/backend/data/topic.md
# 按你的业务模块编辑 topic.md
```

## 支持的 LLM Provider

| Provider | 变量 | 说明 |
|----------|------|------|
| 智谱 AI (GLM) | `ZHIPU_API_KEY` | 默认，国内首选 |
| MiniMax | `MINIMAX_API_KEY` | 中文推理能力强 |
| 阿里云百炼 (Qwen) | `ALIYUN_API_KEY` | 国内稳定 |
| OpenAI | `OPENAI_API_KEY` | 国际首选 |
| Anthropic Claude | `ANTHROPIC_API_KEY` | 长文本分析 |
| SiliconFlow | `SILICONFLOW_API_KEY` | 开源模型中转 |
| Gemini | `GEMINI_API_KEY` | Google AI |

## 架构概览

```
┌─────────────────────────────────────┐
│        浏览器 (纯前端 HTML/JS)        │
└────────────┬────────────────────────┘
             │ HTTP API
┌────────────▼────────────────────────┐
│     FastAPI 后端 (uvicorn, 单 worker) │
│  ┌─────────┐ ┌──────┐ ┌──────────┐  │
│  │ board   │ │report│ │  agents  │  │
│  │ service │ │ API  │ │ (JobMstr)│  │
│  └────┬────┘ └──┬───┘ └────┬─────┘  │
│       │         │          │        │
│  ┌────▼─────────▼──────────▼──────┐ │
│  │  ChromaDB (向量)  SQLite (状态) │ │
│  └────────────────────────────────┘ │
└─────────────────────────────────────┘
          │ JQL API
  ┌───────▼────────┐
  │   Jira Server  │
  └────────────────┘
```

详见 [docs/project-overview.md](docs/project-overview.md)

## 路线图

- [ ] 多租户支持（同一实例管理多个 Jira 项目）
- [ ] 工单自动关闭建议
- [ ] Confluence KB 导入
- [ ] 原生钉钉/企微机器人
- [ ] REST API 鉴权增强（OAuth2）

在 [GitHub Issues](https://github.com/crosswarm/aiticket/issues) 追踪和投票功能需求。

## 客户端 Skill（Claude Code 集成）

部署完成后，团队工程师可以把 `client-skill/aiticket/` 安装到自己的 Claude Code，
通过斜杠命令远程访问本实例：

```bash
cp -r client-skill/aiticket ~/.claude/skills/aiticket
```

安装后用 `/aiticket-login` 绑定服务器，即可使用 `/aiticket-search`、`/aiticket-reply`、`/aiticket-kb` 等命令。
详见 [client-skill/aiticket/SKILL.md](client-skill/aiticket/SKILL.md)。

## 贡献

欢迎 PR！请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

适合新手的任务标有 [`good first issue`](https://github.com/crosswarm/aiticket/labels/good%20first%20issue)。

## 许可证

[GNU Affero General Public License v3.0](LICENSE)

AGPL 要求：对修改版本进行网络部署时，需公开修改后的源代码。纯内部使用（不对外提供服务）不受此约束。
