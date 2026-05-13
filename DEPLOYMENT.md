# aiticket 部署配置手册

## 目录

1. [服务器要求](#服务器要求)
2. [依赖安装](#依赖安装)
3. [目录结构与数据持久化](#目录结构与数据持久化)
4. [环境变量配置](#环境变量配置)
5. [首次启动](#首次启动)
6. [Nginx 反代配置](#nginx-反代配置)
7. [systemd 服务配置](#systemd-服务配置)
8. [Skill 客户端绑定](#skill-客户端绑定)
9. [升级](#升级)
10. [常见问题](#常见问题)

---

## 服务器要求

### 最低配置

| 项目 | 要求 |
|---|---|
| CPU | 2 核 |
| 内存 | 4 GB（嵌入模型首次加载约占 1.5 GB） |
| 磁盘 | 20 GB（KB 知识库 + ChromaDB + 日志） |
| OS | Linux x86_64（Ubuntu 20.04 / CentOS 7+ / Debian 11+）|
| Python | 3.11 或 3.12 |
| 网络 | 能访问 Jira 服务器 + LLM API 端点 |

### 推荐配置（生产）

| 项目 | 推荐 |
|---|---|
| CPU | 4 核 |
| 内存 | 8 GB |
| 磁盘 | 100 GB（SSD，高频 KB 写入场景） |

### 端口占用

| 端口 | 用途 | 备注 |
|---|---|---|
| 18000 | 后端 API（uvicorn） | 可通过 `PORT` 变量修改 |
| 443 / 80 | 对外 HTTPS/HTTP | 由 nginx 反代到 18000 |

---

## 依赖安装

```bash
# 1. 克隆仓库
git clone https://your-git-repo/aiticket-deployable.git /opt/aiticket
cd /opt/aiticket

# 2. 创建虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate

# 3. 安装 Python 依赖
pip install -r APP/backend/requirements.txt
```

> **离线环境**：先在有网机器执行 `pip download -r requirements.txt -d ./wheels`，
> 然后在目标机器执行 `pip install --no-index --find-links=./wheels -r requirements.txt`。

---

## 目录结构与数据持久化

```
/opt/aiticket/           ← 代码根目录（可 git pull 升级）
├── APP/
│   ├── backend/
│   └── frontend/
├── .env                 ← 敏感配置，不进 Git
└── DEPLOYMENT.md

/data/                   ← 数据根目录（DATA_DIR，独立于代码）
├── sqlite/              ← SQLite 数据库
│   ├── app_auth.db      ← 用户认证 / Skill Token
│   ├── kb_jobs.db       ← KB 写入任务队列
│   └── ...
├── chroma/              ← ChromaDB 向量库（KB 语义索引）
├── reply_trainer/       ← 智能回复训练数据
├── agents/              ← Agent 记忆 / 状态
└── schedules/           ← JobMaster 定时任务配置
```

**Docker 部署时挂载 volume：**

```bash
docker run -d \
  --name aiticket \
  -p 18000:18000 \
  -v /host/data:/data \
  --env-file /host/.env \
  aiticket:latest
```

**裸机部署时创建数据目录：**

```bash
mkdir -p /data
chown -R www-data:www-data /data   # 或你的运行用户
```

---

## 环境变量配置

复制模板并填写：

```bash
cp .env.example .env
vim .env
```

### 必填变量

| 变量 | 说明 | 示例 |
|---|---|---|
| `APP_SECRET_KEY` | Cookie 签名密钥（生产必须修改） | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `SKILL_TOKEN_SALT` | Skill Token 签名盐 | `python -c "import secrets; print(secrets.token_hex(16))"` |
| `JIRA_BASE_URL` | Jira 服务器地址（不含末尾斜杠） | `https://jira.mycompany.com` |
| `LLM_DEFAULT_PROVIDER` | 默认 LLM Provider | `zhipu` / `openai` / `aliyun` 等 |
| `{PROVIDER}_API_KEY` | 对应 Provider 的 API Key | 见 .env.example |

### 常用可选变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `18000` | 服务监听端口 |
| `DATA_DIR` | `/data` | 数据持久化目录 |
| `NOTIFICATION_WEBHOOK_URL` | — | 飞书 / WeCom / 钉钉 Webhook |
| `NOTIFICATION_WEBHOOK_FORMAT` | `feishu` | `feishu` / `wecom` / `dingtalk` / `generic` |
| `JIRA_SSL_VERIFY` | `true` | 自签名证书时设为 `false` 或 CA 路径 |
| `JIRA_CA_BUNDLE` | — | 企业内网 CA 证书路径 |
| `PM_BASE_URL` | — | PM 协作系统 API 地址（不用 PM 模块可留空） |
| `ENABLE_SCHEDULER` | `true` | JobMaster 定时任务开关 |
| `ALLOW_EMBEDDING_DOWNLOAD` | `true` | 离线环境设为 `false` |
| `CHROMA_MODE` | `persistent` | `persistent`（落盘）/ `in-memory`（测试） |

完整变量列表见 [`.env.example`](./.env.example)。

---

## 首次启动

```bash
cd /opt/aiticket
source .venv/bin/activate

# 加载环境变量（或使用 systemd EnvironmentFile）
set -a; source .env; set +a

# 启动后端
cd APP/backend
uvicorn main:app --host 0.0.0.0 --port ${PORT:-18000} --workers 1

# 验证
curl http://127.0.0.1:18000/api/health
# 期望：{"status": "ok", ...}
```

> **重要**：`--workers` 必须为 `1`。ChromaDB 使用文件锁，多 worker 会导致锁冲突崩溃。

首次启动会自动完成：
- SQLite schema 初始化
- 嵌入模型下载（约 400 MB，需要访问 HuggingFace；离线环境提前放置）
- 定时任务注册

---

## 管理员账号初始化

后端首次启动后，通过接口创建第一个管理员账号：

```bash
curl -X POST http://127.0.0.1:18000/api/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password"}'
```

> 若 `/api/auth/setup` 已被锁定（已有用户），则此接口返回 403。

---

## Nginx 反代配置

```nginx
server {
    listen 443 ssl;
    server_name aiticket.yourteam.com;

    ssl_certificate     /etc/ssl/certs/aiticket.crt;
    ssl_certificate_key /etc/ssl/private/aiticket.key;

    # 上传文件大小限制（KB 文档上传，最大 20 MB）
    client_max_body_size 25m;

    location / {
        proxy_pass         http://127.0.0.1:18000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # SSE 流式响应（智能回复生成）
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
    }
}

# HTTP → HTTPS 跳转
server {
    listen 80;
    server_name aiticket.yourteam.com;
    return 301 https://$host$request_uri;
}
```

---

## systemd 服务配置

```ini
# /etc/systemd/system/aiticket.service

[Unit]
Description=AITicket Backend
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/aiticket/APP/backend
EnvironmentFile=/opt/aiticket/.env
ExecStart=/opt/aiticket/.venv/bin/uvicorn main:app \
    --host 0.0.0.0 \
    --port 18000 \
    --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable aiticket
systemctl start aiticket
systemctl status aiticket
```

查看日志：

```bash
journalctl -u aiticket -f
```

---

## Skill 客户端绑定

部署完成后，用户在各自的 Claude Code 环境中绑定：

1. 复制 Skill 目录到用户机器：
   ```bash
   cp -r client-skill/aiticket ~/.claude/skills/aiticket
   pip install requests pyyaml
   ```

2. 从 Web UI 获取 Skill Token：`账号设置 → Skill Token → 生成`

3. 在 Claude Code 中运行：
   ```
   /aiticket-profile-add my-server
   ```
   按提示填入：
   - `api_base`：`https://aiticket.yourteam.com`
   - `token`：从 Web UI 复制的 Token
   - `default_project`：Jira 项目 Key（如 `MYPROJECT`）

4. 验证连接：
   ```
   /aiticket-search 测试
   ```

---

## 升级

```bash
cd /opt/aiticket

# 1. 停服务
systemctl stop aiticket

# 2. 拉取新版本
git pull

# 3. 更新依赖（如有变更）
source .venv/bin/activate
pip install -r APP/backend/requirements.txt

# 4. 重启（schema 迁移在启动时自动执行）
systemctl start aiticket
```

> **注意**：`DATA_DIR` 中的数据库和向量库不会被 `git pull` 覆盖，升级安全。

---

## 常见问题

### 启动报 `ChromaDB lock` / 后端卡死

已有僵尸进程占用 ChromaDB 文件锁。

```bash
# 查找并杀死僵尸进程
ps aux | grep uvicorn
kill -9 <PID>

# 再启动
systemctl start aiticket
```

### 嵌入模型下载失败（离线环境）

1. 在有网机器下载 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 模型文件
2. 放置到 `{DATA_DIR}/models/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/`
3. 设置 `ALLOW_EMBEDDING_DOWNLOAD=false`

### Jira 自签名证书报错

```
# 方案 A（不推荐生产）：跳过验证
JIRA_SSL_VERIFY=false

# 方案 B（推荐）：指定 CA 证书
JIRA_CA_BUNDLE=/etc/ssl/certs/my-internal-ca.crt
```

### LLM 调用超时

检查服务器到 LLM API 端点的网络连通性：

```bash
curl -v https://open.bigmodel.cn/api/paas/v4/chat/completions \
  -H "Authorization: Bearer $ZHIPU_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-4-flash","messages":[{"role":"user","content":"hi"}]}'
```

若通过代理访问，确认 `https_proxy` / `no_proxy` 环境变量设置正确。

### 上传 KB 文档报 413

nginx `client_max_body_size` 未设置或设置过小，改为 `25m`（见上方 nginx 配置）。
