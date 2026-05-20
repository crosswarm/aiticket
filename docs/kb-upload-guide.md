# 知识库上传与解析手册

AITicket 支持将企业内部文档导入知识库（KB），AI 回复时会自动检索相关文档片段，生成基于知识库的准确回复。

---

## 支持的文件格式

| 格式 | 说明 |
|------|------|
| `.md` | Markdown（原生支持，推荐格式）|
| `.txt` | 纯文本 |
| `.pdf` | PDF 文档（通过 markitdown 转换）|
| `.docx` | Word 文档（通过 markitdown 转换）|
| `.xlsx` | Excel 表格（通过 markitdown 转换，按行提取文本）|

> `.pdf`、`.docx`、`.xlsx` 会先自动转换为 Markdown，再进行向量化索引。转换时会尽量保留标题层级和表格结构。

---

## 上传方式

### 方式一：直接放入 KB/ 目录（推荐）

```bash
# 将文档放入 KB/ 目录（支持子目录组织）
cp /path/to/your/doc.md KB/your-category/doc.md
cp /path/to/policy.pdf  KB/hr/policy.pdf

# 触发索引（扫描新文件并写入向量库）
curl -X POST http://localhost:18000/api/kb/sync
```

目录结构示例：

```
KB/
├── hr/
│   ├── 01_attendance_policy.txt
│   ├── 02_leave_guide.md
│   └── 03_salary.pdf
├── product/
│   ├── feature_overview.md
│   └── release_notes.md
└── faq/
    └── common_issues.md
```

### 方式二：使用导入脚本

```bash
# 扫描 KB/ 目录，自动转换非 .md 格式并建索引
python scripts/import_kb.py

# 强制全量重建（清除旧索引）
python scripts/import_kb.py --reset
```

### 方式三：通过 Claude Code Skill 上传

如果已安装 `client-skill/aiticket`，可直接在 Claude Code 中上传：

```
/aiticket-kb-upload /path/to/doc.pdf
```

详见 [Skill 使用手册](client-skill-guide.md)。

---

## 索引原理

1. **扫描**：遍历 `KB/` 目录，读取所有支持格式的文件
2. **转换**：非 `.md` 格式通过 markitdown 转换为 Markdown
3. **分块**：将文档按段落/标题切分为 chunk（默认约 500 字）
4. **向量化**：使用 embedding 模型（`paraphrase-multilingual-MiniLM-L12-v2`）为每个 chunk 生成向量
5. **存储**：向量写入 ChromaDB，文本存入 SQLite 索引

**索引位置**（本地开发）：
- ChromaDB：`APP/backend/chroma_db/`
- SQLite 索引：`APP/backend/data/sqlite/`

---

## KB 检索调试

### 验证检索是否正常

```bash
TOKEN=<admin-token>

# 搜索"年假天数"，查看返回的文档片段
curl -G "http://localhost:18000/api/kb/search" \
  --data-urlencode "q=年假天数" \
  -H "Authorization: Bearer $TOKEN"
```

期望返回：

```json
{
  "results": [
    {
      "content": "根据《职工带薪年休假条例》及公司规定，年假天数按工龄计算...",
      "source": "KB/hr/02_leave_guide.md",
      "score": 0.87
    }
  ]
}
```

### 验证智能回复引用 KB

```bash
curl -X POST http://localhost:18000/api/board/generate-reply \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"issue_key": "your-issue-key"}'
```

回复中应包含从 KB 文档中提取的具体信息（如具体天数、流程步骤等）。

---

## 常见问题

### 中文内容乱码或无法检索

1. 确认文件编码为 UTF-8（TXT/MD 文件）
2. PDF 文档包含扫描图片而非文字时，markitdown 无法提取文本；建议转为可选中文字的 PDF 或 Word
3. 索引后通过 `/api/kb/search` 验证是否能返回内容

### PDF 表格内容丢失

markitdown 对复杂嵌套表格的支持有限。建议：
- 将关键表格内容复制到 Markdown 文件作为补充
- 或在 PDF 中保存一份对应的 `.md` 版本（同名，优先索引 `.md`）

### 文件大小限制

- 单文件建议不超过 2MB
- 超大文件（如包含大量图片的 Word）建议拆分为多个小文件
- 纯文字内容：无实际大小限制

### 索引后检索不到新文件

1. 确认已执行 `POST /api/kb/sync` 或 `python scripts/import_kb.py`
2. 检查文件是否在 `KB/` 目录内（不是其他路径）
3. 查看 sync 返回结果中是否有该文件：

```bash
curl -X POST http://localhost:18000/api/kb/sync | python3 -m json.tool
```

### 向量模型首次下载慢

系统首次运行时会自动下载 embedding 模型（约 300MB）。如果网络受限，可以：

```bash
# 预先下载到本地
pip install sentence-transformers
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

# 或在 .env 中设置离线模式（不允许下载）
ALLOW_EMBEDDING_DOWNLOAD=false
```

---

## 全量重建索引

当 KB 内容大量变化时，建议全量重建：

```bash
# 方式一：通过脚本
python scripts/import_kb.py --reset

# 方式二：删除旧索引目录后重新 sync
rm -rf APP/backend/chroma_db/
curl -X POST http://localhost:18000/api/kb/sync
```

全量重建时间：约 1-5 分钟（取决于文档数量和机器性能）。

---

## KB 维护建议

- 按主题组织子目录（如 `KB/hr/`、`KB/product/`、`KB/faq/`）
- 文件名使用序号前缀（如 `01_`、`02_`）保持有序
- Markdown 格式使用清晰的标题层级（`#`、`##`、`###`）提升检索精度
- 文档更新后及时重新 sync，避免 AI 引用过时内容
