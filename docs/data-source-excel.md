# Excel / CSV 数据源接入手册

AITicket 支持从 Excel (.xlsx) 或 CSV 文件直接读取工单数据，无需部署 Jira。适合：

- 从现有工单系统导出数据进行离线分析
- 轻量化部署（单机 / 内网环境）
- Demo 或培训场景
- 与 Jira 并行的数据验证

---

## 快速开始

### 1. 准备 Excel 文件

文件格式要求：
- 格式：`.xlsx`（推荐）或 `.csv`
- 第一行为列标题
- 每行一条工单
- 字符编码：UTF-8（CSV）或标准 xlsx

### 2. 配置 deployment.yaml

编辑 `APP/backend/config/deployment.yaml`，添加以下内容：

```yaml
instance:
  name: "我的工单系统"
  slug: "myticket"
  primary_project_key: "TICKET"

data_source:
  type: excel
  excel:
    file_path: "data/imports/my_tickets.xlsx"
    column_map:
      key:         "工单编号"      # 必填：唯一标识
      summary:     "问题标题"      # 必填：工单标题
      description: "问题描述"      # 推荐：详细描述
      status:      "当前状态"      # 推荐：工单状态
      assignee:    "处理人"        # 可选
      reporter:    "提出者"        # 可选
      priority:    "优先级"        # 可选
      created:     "提出时间"      # 可选：创建时间
      issue_type:  "工单类型"      # 可选：类型分类
      project_name: "所属项目"     # 可选：项目/模块归属
```

### 3. 将文件放到正确位置

```bash
cp /path/to/your/tickets.xlsx data/imports/my_tickets.xlsx
```

### 4. 启动或重启服务

```bash
# 本地开发
cd APP/backend && uvicorn main:app --reload --port 18000

# Docker
docker compose restart aiticket
```

### 5. 验证

```bash
curl http://localhost:18000/api/board/stats
# → {"issues_count": N, "source": "excel", ...}
```

---

## 字段说明

### 必填字段

| column_map 键 | 说明 | 示例值 |
|---------------|------|--------|
| `key` | 工单唯一 ID | `TICKET-001`、`T-001`、`20240501-001` |
| `summary` | 工单标题 | `无法登录系统`、`申请年假` |

### 推荐字段

| column_map 键 | 说明 | 示例值 |
|---------------|------|--------|
| `description` | 详细描述（AI 回复依赖此字段） | 完整的问题描述文本 |
| `status` | 当前状态 | `待处理`、`处理中`、`已解决` |
| `assignee` | 处理人 | `张三`、`customer-support` |

### 可选字段

| column_map 键 | 说明 |
|---------------|------|
| `reporter` | 提报人姓名 |
| `contact_info` | 提报人联系方式 |
| `customer_name` | 客户/公司名称（显示在工单卡片）|
| `priority` | 优先级（高/中/低 或 P0/P1/P2）|
| `created` | 创建时间（字符串或 Excel 日期格式均可）|
| `issue_type` | 工单类型（咨询/申请/投诉/Bug/需求）|
| `project_name` | 所属项目或业务模块 |

### extra 字段

不在上述列表中的列会自动存入工单的 `extra` 字段，以 JSON 形式保留，可在工单详情页查看，也可用于 AI 提示词上下文。例如：

```yaml
column_map:
  key: "工单编号"
  # 自定义字段会进入 extra
  customer_level: "客户等级"    # 进入 issue.extra["customer_level"]
  region: "所在区域"             # 进入 issue.extra["region"]
```

---

## 数据格式注意事项

### 日期列

系统兼容多种日期格式：

```
2024-05-15 09:23        ← 推荐（ISO 格式字符串）
2024/05/15 09:23
2024-05-15
Excel 原生日期格式      ← 也支持（openpyxl 自动转换）
```

建议在保存 xlsx 时将日期列设置为文本格式，避免跨版本解析问题。

### 空值处理

- `key` 为空的行会被跳过
- 其他字段为空时使用默认值：`status` 默认 `"未知"`，`priority` 默认 `"中"`
- 保留 `None` 值，不会引发加载错误

### 换行符

`description` 列中包含换行符（`\n` 或 `\r\n`）是正常的，系统会保留原始格式。

---

## 增量更新

替换文件后触发重新读取（无需重启）：

```bash
# 删除 Excel 板缓存，下次请求时自动重新读取
rm -f APP/backend/data/cache/excel_board.json

# 或通过 API 触发
curl -X POST http://localhost:18000/api/admin/refresh-cache \
  -H "Authorization: Bearer <token>"
```

---

## 与 Jira 模式对比

| 特性 | Excel 模式 | Jira 模式 |
|------|-----------|----------|
| 实时数据 | 静态（手动更新文件）| 实时（直接查询 Jira）|
| 评论 / 附件 | 不支持 | 支持 |
| 自动同步 | 不支持 | 支持（增量 + 全量）|
| 部署复杂度 | 极低 | 需要 Jira 服务器访问权限 |
| 适用场景 | 离线分析、Demo | 生产环境 |

---

## 接入 Checklist

在正式使用前，建议完成以下检查：

- [ ] Excel 文件能被 pandas 正常读取（`pd.read_excel("your_file.xlsx")` 不报错）
- [ ] `key` 列无重复值
- [ ] `description` 列内容足够详细（AI 回复质量依赖此字段）
- [ ] 配置文件中 `file_path` 路径正确（相对于 `APP/backend/` 目录）
- [ ] `column_map` 中的列名与文件实际列名完全一致（区分大小写）
- [ ] 时间格式能被正常解析（检查看板是否显示创建时间）
- [ ] `issues_count` 与 Excel 行数一致（检查有无行被跳过）
- [ ] 至少一条工单能生成智能回复

---

## 排错

**问题：`issues_count = 0`**
- 检查 `file_path` 是否正确，路径相对于 `APP/backend/` 目录
- 检查 Excel 文件第一行是否为标题行
- 查看后端日志：`tail -f APP/backend/logs/app.log`

**问题：智能回复为空或报错**
- `description` 列可能为空或太短，AI 无法生成回复
- 检查 LLM 配置（`config/deployment.yaml` 的 `llm` 段或 `.env` 中的 API Key）

**问题：中文列名乱码**
- 确认 xlsx 文件保存时使用了 UTF-8 编码（或标准 xlsx 格式）
- 如果是 CSV，保存时选择 UTF-8 with BOM 格式
