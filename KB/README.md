# KB 知识库目录

此目录存放用于 AI 语义检索的知识库文档。

## 如何使用

1. 将你的 Markdown 文档放入此目录（支持子目录）
2. 运行导入脚本建立向量索引：

```bash
docker compose exec backend python scripts/import_kb.py
```

## 支持的文档格式

- `.md` / `.txt`：直接导入
- `.pdf`：需安装 `pdfplumber`（见 requirements-extras.txt）

## 示例文档

参考 `samples/kb/` 目录中的示例文档。

## 注意

此目录的内容**不纳入 Git 版本控制**（已在 `.gitignore` 中排除）。
请自行备份或通过 CI/CD 挂载。
