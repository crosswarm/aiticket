# 知识库维护手册

适用对象：各部门的 **KB 管理员**（1-2 人）。无需了解代码，只需会访问服务器文件目录和调用 API。

---

## 一、核心概念（3 分钟读完）

知识库有 5 种内容来源（source_kind），日常维护主要用前 3 种：

| 来源 | 怎么进来的 | 适合放什么 |
|---|---|---|
| `doc` | 你手动放文件 + 触发同步 | 产品文档、操作手册、FAQ、SOP |
| `kb_compiled` | AI 自动综合现有文档生成 | 某个话题的结构化解析（如「审批矩阵」的全面总结） |
| `user_contributed` | 处理工单时被动自动采集 | 每天回复工单时沉淀的解决方案片段 |
| `ticket_case` | 从已解决工单中提取 | 历史工单案例 |
| `fact` | `data/product_facts.md` 手动维护 | 可直接引用的产品约束/限制/行为边界 |

---

## 二、文件目录结构

```
data/                          ← 挂载的数据卷（docker-compose 的 volume）
└── kb/                        ← 知识库根目录
    ├── 流程管理/               ← 一级分类（l1_module）= 文件夹名
    │   ├── 审批流/             ← 二级分类（l2_module）= 子文件夹名
    │   │   ├── 审批矩阵配置.md
    │   │   └── 撤回规则.md
    │   └── 流程设计器使用手册.md
    ├── 基础配置/
    │   └── 字段权限说明.md
    └── INDEX/                 ← 系统自动生成，勿手动改
        └── manifest.json
```

**命名规则：**
- 文件夹名 = 知识分类（中文或英文均可，会在搜索结果中展示）
- 文件支持格式：`.md`、`.txt`、`.csv`、`.html`
- 文件名即知识条目名称，建议直接用功能名称命名（如 `字段权限配置说明.md`）

---

## 三、日常操作

### 3.1 添加/更新一篇文档（最常用）

```bash
# 1. 把文件放到对应分类目录
cp 你的文档.md /部署目录/data/kb/你的分类/你的文档.md

# 2. 触发同步（任选一种）
#    方式A：curl 命令
curl -X POST http://你的服务地址/api/kb/sync -b "session=你的cookie"

#    方式B：网页操作
# 登录 → 知识库页面 → 右上角「同步知识库」按钮
```

同步完成后，搜索即可命中新文档。**同步不会清除 AI 编译条目**（有保护机制）。

### 3.2 配置话题树（首次部署 + 分类调整时）

话题树决定了 AI 编译时的主题划分，文件路径：

```
data/notes/topic.md   （或 deployment.yaml 中 kb.topic_file 指向的路径）
```

格式示例：
```markdown
# 流程管理
## 审批流
### 审批矩阵
### 撤回与重置
## 流程设计器
### 连线规则
### 条件分支
# 基础配置
## 字段权限
## 组织管理
```

层级 = H1 (一级) / H2 (二级) / H3 (具体话题)。AI 会按叶节点话题名来综合编译 KB 条目。

### 3.3 触发 AI 编译（新增大量文档后运行一次）

```bash
# 编译全部未编译话题（后台异步，按话题数量需 10-60 分钟）
curl -X POST http://你的服务地址/api/kb/compile-all \
  -H "Content-Type: application/json" \
  -b "session=你的cookie"

# 只编译指定话题（快速补充单个话题）
curl -X POST http://你的服务地址/api/kb/compile \
  -H "Content-Type: application/json" \
  -d '{"topic": "审批矩阵"}' \
  -b "session=你的cookie"

# 查询编译进度
curl http://你的服务地址/api/kb/jobs?limit=10 -b "session=你的cookie"
```

编译完成后，在 KB 搜索页可以看到「综合解析：审批矩阵」这类条目，内容是 AI 跨文档综合的结构化总结。

### 3.4 删除一篇文档

```bash
# 1. 删除文件
rm /部署目录/data/kb/分类/文档名.md

# 2. 重新同步（会清除已删文件的索引）
curl -X POST http://你的服务地址/api/kb/sync -b "session=你的cookie"
```

### 3.5 删除 AI 编译条目

```bash
# 先查出 content_id
curl "http://你的服务地址/api/kb/compiled?top_k=100" -b "session=你的cookie" | \
  python3 -c "import sys,json; [print(i['content_id'], i['name']) for i in json.load(sys.stdin)['items']]"

# 再删除
curl -X DELETE "http://你的服务地址/api/kb/compiled/综合解析-审批矩阵" \
  -b "session=你的cookie"
```

---

## 四、初次建库（部署后 Day 1）

推荐操作顺序：

```
1. 整理现有文档
   ├── 收集产品手册、操作说明、内部 Wiki 文章
   ├── 转成 .md 或 .txt 格式（Word 可用 pandoc 转：pandoc -o 文档.md 文档.docx）
   └── 按业务模块建好子目录

2. 批量放入 data/kb/

3. 配置 topic.md（参考文档目录结构来划分话题）

4. 触发首次同步
   curl -X POST http://你的服务地址/api/kb/sync

5. 确认同步成功
   curl http://你的服务地址/api/kb/manifest | python3 -c "
   import sys,json; m=json.load(sys.stdin)
   print('文档数:', len(m['items']), '话题数:', len(m.get('topics',[])))
   "

6. 触发 AI 全量编译
   curl -X POST http://你的服务地址/api/kb/compile-all

7. 测试搜索效果
   curl "http://你的服务地址/api/kb/search?q=审批矩阵&project_key=你的项目键"
```

---

## 五、日常维护节奏

| 频率 | 操作 | 说明 |
|---|---|---|
| 随时 | 放文件 + sync | 有新文档就加，立即生效 |
| 每周 | compile-all | 让 AI 重新综合最新内容 |
| 每月 | 健康检查 | 查看覆盖率和缺失话题 |
| 季度 | 清理过期条目 | 删除已下线功能的文档 |

**健康检查命令：**

```bash
# KB 覆盖率 + 缺失话题报告
curl -X POST http://你的服务地址/api/kb/lint -b "session=你的cookie"

# 查看各类型条目数量
curl http://你的服务地址/api/kb/compiled-health -b "session=你的cookie"
```

---

## 六、多项目场景

如果同一实例管理多个 Jira 项目，文档入库时可以在目录层级区分，也可以在 deployment.yaml 中配置 `allowed_project_keys`。

搜索时传 `project_key` 可以限定只搜该项目的内容：

```bash
# 只搜 PROJ1 相关的 KB（同时返回 _global 全局内容）
curl "http://你的服务地址/api/kb/search?q=工作流&project_key=PROJ1"
```

如果文档是通用的（适用所有项目），不需要任何特殊处理，默认即为全局可见（`project_key = _global`）。

---

## 七、常见问题

**Q: 同步后搜索不到新文档？**
检查文件格式是否在支持列表（.md/.txt/.csv/.html/.xml），文件名不含特殊字符，然后确认 sync 返回了 `"ok": true`。

**Q: AI 编译一直 pending？**
检查后台 LLM 服务是否正常：`curl http://你的服务地址/api/llm/health`。也可查看 job 状态：`/api/kb/jobs/{job_id}`。

**Q: sync 之后 AI 编译的条目消失了？**
系统有自动保护机制，sync 后如发现条目减少会从备份恢复。如需手动触发：
```bash
curl -X POST http://你的服务地址/api/kb/restore-compiled -b "session=你的cookie"
```

**Q: 如何批量导入历史工单案例？**
历史已解决工单通过看板的「工单同步」功能导入，系统会自动提取有价值的解决方案作为 `ticket_case` 入库。

**Q: product_facts.md 是什么？**
这是产品约束事实的专用文件（如「审批矩阵单层最多支持 200 个审批人」），由管理员手动维护。路径：`APP/backend/data/product_facts.md`，格式为 Markdown 列表，每行一条事实。
