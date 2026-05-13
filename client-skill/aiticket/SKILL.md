---
name: aiticket
description: >
  当用户询问工单/issue、需要生成智能回复、或要搜索知识库时使用。
  连接到自托管的 aiticket 实例。可用斜杠命令：
  /aiticket-login /aiticket-profile-add
  /aiticket-profile-list /aiticket-profile-use /aiticket-profile-remove
  /aiticket-search /aiticket-reply /aiticket-kb /aiticket-kb-upload
  /aiticket-switch-project
---

# AITicket 客户端 Skill

通过 REST API 连接到自托管的 aiticket 实例。

## 一次性配置

1. 把 skill 复制到本地 skills 目录：
   ```bash
   cp -r client-skill/aiticket ~/.claude/skills/aiticket
   ```
2. 在 Web 界面获取 API token：**账号设置 → Skill Token → 生成**
3. 运行 `/aiticket-profile-add <名称>` 进行引导式配置。

或手动创建 `~/.claude/skills/aiticket/profiles.json`：
```json
{
  "default": "my-server",
  "profiles": {
    "my-server": {
      "api_base": "https://aiticket.yourteam.com",
      "token": "your-skill-token-here",
      "default_project": "YOUR_PROJECT_KEY"
    }
  }
}
```

## 斜杠命令

### Profile 管理

| 命令 | 说明 | 示例 |
|---|---|---|
| `/aiticket-login` | 首次绑定（profile-add 的别名，profile 名为 default） | `/aiticket-login` |
| `/aiticket-profile-add <name>` | 添加或更新服务端 Profile | `/aiticket-profile-add company-a` |
| `/aiticket-profile-list` | 列出所有 Profiles（token 脱敏） | `/aiticket-profile-list` |
| `/aiticket-profile-use <name>` | 切换默认 Profile | `/aiticket-profile-use company-b` |
| `/aiticket-profile-remove <name>` | 删除 Profile | `/aiticket-profile-remove old-server` |

### 工单 & 知识库

| 命令 | 说明 | 示例 |
|---|---|---|
| `/aiticket-search <query>` | 搜索 issue（支持 JQL 或关键词） | `/aiticket-search 审批流退回` |
| `/aiticket-reply <issue-key>` | 为指定 issue 生成智能回复 | `/aiticket-reply EXAMPLE-1001` |
| `/aiticket-kb <query>` | 查询知识库 | `/aiticket-kb 工作流超时配置` |
| `/aiticket-kb-upload <file>` | 上传文档到知识库 | `/aiticket-kb-upload /tmp/guide.md --project=EXAMPLE` |
| `/aiticket-switch-project <key>` | 切换当前项目 | `/aiticket-switch-project EXAMPLE` |

### 多 Profile 用法

所有命令都接受 `--profile <名称>` 指定要操作的服务器：
```
/aiticket-search 审批流 --profile company-b
/aiticket-kb-upload guide.md --profile staging
```

## 如何调用

每个命令对应运行 `scripts/` 下的一个 Python 脚本。
脚本依赖 Python 3.11+ 与 `requests` 包。

一次性安装依赖：
```bash
pip install requests pyyaml
```

## 工作流

1. 运行 `/aiticket-profile-add my-server` 保存凭据
2. 用 `/aiticket-search` 查找相关工单
3. 用 `/aiticket-reply <工单号>` 起草回复 — Claude 会收到生成内容并可继续打磨
4. 用 `/aiticket-kb` 把知识库内容引入对话
5. 用 `/aiticket-kb-upload <文件>` 上传文档到知识库

## 知识库上传细节

支持格式：`.md` `.txt` `.csv` `.html` `.xml` `.pdf` `.docx` `.pptx` `.xlsx`
单文件最大：20 MB
文档按 project_key 隔离，上传后立即可搜索。

```bash
/aiticket-kb-upload /path/to/manual.pdf --title="操作手册 v3" --project=EXAMPLE --topic="工作流"
```
