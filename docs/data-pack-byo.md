# 自建资料包导入指南

适用对象：想把**自己公司的内部文档**也加进 KB 知识库的客户。

> 如果你是第一次部署官方资料包，先看 [data-pack-deploy.md](data-pack-deploy.md)。
> 想了解自建内容和官方内容如何安全并存、官方升级不丢失数据，见 [data-pack-coexistence.md](data-pack-coexistence.md)。

---

## 核心规则：使用 `_local/` 命名空间

为了让你的文档不被官方资料包更新覆盖，**所有自建内容放在 `KB/_local/` 子树下**：

```
KB/
├── 打印/          ← 官方目录，勿放私有文档
├── 流程中心/       ← 官方目录
├── ...
└── _local/          ← 你的专属空间，官方包永不触碰
    ├── 财务流程/
    │   ├── 报销审批流程.md
    │   └── 预算申请指南.md
    ├── HR制度/
    │   └── 入职离职流程.md
    └── 产品知识/
        └── 我们产品的常见问题.md
```

> `_local/` 是约定，不是代码限制。凡是不在官方 INVENTORY 中的目录，tar 升级都不会碰到。但 **最安全的做法是统一使用 `_local/` 前缀**，让意图清晰。

---

## 文件格式支持

| 格式 | 支持 | 备注 |
|------|------|------|
| `.md` | ✅ | 推荐。支持标题/列表/表格 |
| `.txt` | ✅ | 纯文本 |
| `.csv` | ✅ | 每行作为一条知识 |
| `.html` | ✅ | 会自动剥离 HTML 标签 |
| `.pdf` | 需额外安装 | 见下方 FAQ |
| `.docx` / `.xlsx` | 需 builder 预处理 | 见下方"进阶" |

---

## 快速导入：3 步上手

### 第 1 步：把文档放进去

```bash
# 在服务器上的部署目录里操作
mkdir -p /path/to/aiticket/KB/_local/我的分类/

# 复制你的文档（支持批量）
cp 你的文档.md /path/to/aiticket/KB/_local/我的分类/
cp -r 你的文档目录/ /path/to/aiticket/KB/_local/
```

或者通过 scp 从本地传过去：
```bash
scp -r ./我的KB文档/ user@server:/path/to/aiticket/KB/_local/
```

**目录命名即分类名**，中文英文均可，会在搜索结果页面里展示。

### 第 2 步：触发同步

```bash
curl -X POST http://localhost:18000/api/kb/sync -b /tmp/cookies.txt
```

（如果 cookies 过期，重新登录：`curl -X POST http://localhost:18000/api/login -d '{"username":"admin","password":"admin"}' -c /tmp/cookies.txt`）

或者通过网页操作：登录 → 知识库 → 右上角「同步知识库」按钮。

### 第 3 步：验证

```bash
curl "http://localhost:18000/api/kb/search?q=你文档里的关键词" -b /tmp/cookies.txt
```

能看到结果即成功。

---

## 进阶：批量整理文档

### 从 Confluence 导出

Confluence 的「导出空间为 HTML」功能导出后：
1. 拆分为独立的 `.html` 文件（一个页面一个文件）
2. 按主题组织到子目录
3. 放入 `KB/_local/<你的空间名>/`，跑 sync

### 从 Word (.docx) 导入

系统内置了格式转换，先用容器脚本批量转换：

```bash
docker compose exec backend python -c "
from APP.backend.kb_local_builder import KBLocalBuilder
from pathlib import Path
builder = KBLocalBuilder(Path('/app'))
builder.convert_office_files()  # 把 KB/ 下所有 docx/xlsx 转为 md
"
```

转换后的 `.md` 文件落在 `KB/OUTPUT/converted/`，你可以把它们整理移动到 `KB/_local/<分类>/`，再跑 sync。

### 保持文档结构清晰

推荐的目录深度：**2-3 层**（一级 = 业务领域，二级 = 子模块，文件 = 词条）

```
KB/_local/
├── 财务/
│   ├── 报销/
│   │   ├── 报销限额说明.md
│   │   └── 常见驳回原因.md
│   └── 预算管理/
│       └── Q2预算申请流程.md
└── IT支持/
    ├── VPN配置指南.md
    └── 常用系统账号申请.md
```

---

## 打包自建资料包（多机部署时）

如果你有多台服务器需要同步，可以把自建内容也打成包：

```bash
cd /path/to/aiticket/
tar --exclude='.DS_Store' \
    -czf ~/aiticket-data-pack-private-<你的标识>-$(date +%F).tar.gz \
    KB/_local/ conclusion/_local/ design/_local/
```

其他服务器解压：
```bash
tar -xzf aiticket-data-pack-private-<你的标识>-2026-05-13.tar.gz -C /path/to/aiticket/
```

---

## 话题树配置（让 AI 编译效果更好）

话题树决定 AI 如何理解和整合你的文档。如果你的私有 KB 涉及官方话题树里没有的业务领域，建议在 `APP/backend/data/topic.md` 里追加你的模块：

```markdown
# 财务管理（新增）
## 报销审批
### 报销限额
### 驳回规则
## 预算管理
```

修改后重跑 compile-all 让 AI 重新综合。

---

## FAQ

**Q：如果我把文档放进了官方目录（如 `KB/流程中心/my_doc.md`），会怎样？**
- 正常使用没有影响，KB 会检索到它
- 但升级官方资料包时，如果新包也有 `KB/流程中心/` 下的变化，`tar -xzf` 会覆盖同名文件，**不会删除你新加的文件**
- 更安全的方式仍然是放在 `KB/_local/` 下

**Q：支持 PDF 吗？**
安装额外依赖：
```bash
docker compose exec backend pip install pdfplumber
```
然后把 `.pdf` 文件放入目录，跑 builder 的 `convert_office_files()`。

**Q：文档更新了怎么办？**
直接覆盖原文件，重跑 `POST /api/kb/sync` 即可。同步有幂等保护，不会重复导入。

**Q：删除某篇文档？**
删除文件后，调用 `POST /api/kb/sync`。sync 会检测到文件已消失，自动从索引里移除。
