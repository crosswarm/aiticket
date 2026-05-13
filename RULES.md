# 项目规范

- 输出语言：简体中文
- 代码位置：`APP/` 目录
- 中间产物：`conclusion/temp/`

## 文件管理

- 项目根目录禁止随意创建文件（人工维护）
- `KB/`、`conclusion/`、`design/` 为数据目录，不入 git
- 配置文件统一走 `.env`，不硬编码凭据

## Python 规范

- 使用 `pathlib.Path` 代替字符串拼接路径
- 统一 `ruff` 格式化
- 数据目录统一读 `DATA_DIR` 环境变量

## 质量门禁

- 新功能需有对应测试
- PR 合并前 CI 必须全绿
- `--workers` 必须为 1（ChromaDB 文件锁限制）
