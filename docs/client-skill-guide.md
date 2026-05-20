# Claude Code Skill 使用手册

AITicket 提供一个可安装在本地 Claude Code 中的 Skill，让你直接在 Claude 对话里搜索工单、生成智能回复、查询和上传知识库，无需切换浏览器。

---

## 安装

```bash
# 将 skill 复制到 Claude Code skills 目录
cp -r client-skill/aiticket ~/.claude/skills/aiticket
```

如果你从 GitHub 克隆了仓库，`client-skill/aiticket/` 目录已包含完整 Skill。

安装完成后重启 Claude Code，或在当前会话中运行 `/aiticket-login` 即可激活。

---

## 首次配置

### 方式一：引导式配置（推荐）

在 Claude Code 中运行：

```
/aiticket-login
```

或更明确地：

```
/aiticket-profile-add my-server
```

Claude 会询问：
1. AITicket 服务地址（如 `https://aiticket.yourteam.com`）
2. Skill Token（从 Web 界面获取，见下方）
3. 默认项目 Key（如 `EXAMPLE`）

### 获取 Skill Token

1. 打开 AITicket Web 界面
2. 点击右上角头像 → **账号设置**
3. 找到 **Skill Token** 分组 → 点击"生成"
4. 复制 token（仅显示一次，请妥善保存）

### 方式二：手动创建配置文件

```bash
mkdir -p ~/.claude/skills/aiticket
cat > ~/.claude/skills/aiticket/profiles.json << 'EOF'
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
EOF
```

---

## 斜杠命令

### Profile 管理

| 命令 | 说明 | 示例 |
|------|------|------|
| `/aiticket-login` | 首次绑定（`profile-add` 的别名，profile 名为 default）| `/aiticket-login` |
| `/aiticket-profile-add <name>` | 添加或更新服务端 Profile | `/aiticket-profile-add company-a` |
| `/aiticket-profile-list` | 列出所有 Profile（token 脱敏显示）| `/aiticket-profile-list` |
| `/aiticket-profile-use <name>` | 切换默认 Profile | `/aiticket-profile-use company-b` |
| `/aiticket-profile-remove <name>` | 删除 Profile | `/aiticket-profile-remove old-server` |

### 工单操作

| 命令 | 说明 | 示例 |
|------|------|------|
| `/aiticket-search <query>` | 搜索工单（支持关键词或 JQL）| `/aiticket-search 无法登录` |
| `/aiticket-reply <issue-key>` | 为指定工单生成智能回复 | `/aiticket-reply EXAMPLE-1001` |
| `/aiticket-switch-project <key>` | 切换当前默认项目 | `/aiticket-switch-project PROD` |

### 知识库操作

| 命令 | 说明 | 示例 |
|------|------|------|
| `/aiticket-kb <query>` | 查询知识库 | `/aiticket-kb 年假申请流程` |
| `/aiticket-kb-upload <file>` | 上传文档到知识库 | `/aiticket-kb-upload /tmp/guide.md` |

---

## 典型工作流

```
# 1. 搜索相关工单
/aiticket-search 审批流退回

# 2. 查看某条工单的智能回复
/aiticket-reply EXAMPLE-1001

# 3. 如果回复不够准确，先查知识库确认
/aiticket-kb 审批流退回条件

# 4. 上传新文档到知识库
/aiticket-kb-upload /path/to/approval_process.md --project=EXAMPLE
```

Claude 会在对话中直接展示结果，你可以继续追问或让 Claude 进一步加工回复内容。

---

## 知识库上传参数

```
/aiticket-kb-upload <file> [--title="标题"] [--project=KEY] [--topic="主题"]
```

| 参数 | 说明 |
|------|------|
| `<file>` | 本地文件路径，支持 `.md`、`.txt`、`.pdf`、`.docx`、`.xlsx` 等 |
| `--title` | 文档标题（默认使用文件名）|
| `--project` | 关联到指定项目（默认使用 Profile 的 default_project）|
| `--topic` | 文档主题标签 |

单文件最大 20 MB。上传后立即可通过 `/aiticket-kb` 检索。

---

## 多服务器场景

如果你管理多个 AITicket 实例（如测试环境 + 生产环境），可以添加多个 Profile：

```
/aiticket-profile-add staging
/aiticket-profile-add production
```

所有命令都支持 `--profile <名称>` 临时切换：

```
/aiticket-search 登录问题 --profile staging
/aiticket-kb-upload guide.md --profile production
```

切换默认 Profile：

```
/aiticket-profile-use production
```

---

## Token 安全

- Token 存储在本地 `~/.claude/skills/aiticket/profiles.json`，**不会发送到 Anthropic 或其他第三方**
- Token 通过 `Authorization: Bearer <token>` 请求头发送到你自己的 AITicket 服务器
- 如需撤销 token，在 Web 界面 **账号设置 → Skill Token → 重新生成**（旧 token 立即失效）
- 如果 profiles.json 泄露，立即在 Web 界面重新生成 token

---

## 故障排查

### 403 Forbidden

Token 无效或已过期。在 Web 界面重新生成 token，然后更新 Profile：

```
/aiticket-profile-add my-server
# 重新输入服务地址和新 token
```

### 无法连接服务器

检查服务地址是否正确：

```
/aiticket-profile-list
# 确认 api_base 地址，尝试 curl 访问
curl https://aiticket.yourteam.com/api/board/stats
```

常见原因：URL 末尾多了斜杠、HTTPS 证书问题、防火墙端口未开放。

### 404 Not Found

可能是服务版本不匹配。检查服务版本是否支持当前 Skill 要求的接口：

```bash
curl https://aiticket.yourteam.com/api/version
```

### 搜索结果为空

1. 确认 `default_project` 配置正确（区分大小写）
2. 检查工单数据是否已同步（`/api/board/stats` 返回 `issues_count`）
3. 搜索关键词尝试英文或更短的词

### Python 依赖缺失

```bash
pip install requests pyyaml
```

---

## 卸载

```bash
rm -rf ~/.claude/skills/aiticket
```

在 Web 界面撤销 token 以确保安全。
