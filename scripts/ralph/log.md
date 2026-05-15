# Ralph Loop — KB Pipeline Fix Log

Started: 2026-05-16

## Iteration Log

_(自动追加)_

---

## Iter 1 — Layer1-1: _scan_orphans_in_converted

**Story**: KBLocalBuilder 增加 _scan_orphans_in_converted，扫描 KB/OUTPUT/converted/**/*.md 自动补 manifest 缺失 content_id
**Commit**: 3695f04
**Files changed**:
- `APP/backend/kb_local_builder.py` — 新增 `_scan_orphans_in_converted()` 方法 + `build()` 调用
- `docs/user-stories/kb-pipeline-fix.json` — 首次创建（ralph stories）

**验证结果**:
```
# 0 orphans currently (manifest already complete with 989 entries)
python3 -c "from kb_local_builder import KBLocalBuilder; ..."
→ {'source_files': 989, 'converted_files': 989, 'content_count': 989}

# 公式 entries in manifest: 43  (>= 30 required) ✅
```

**Blockers**: 无。DB 目前只有 kb_compiled=98，无 kb_local（等 Layer 1 integration story 触发 sync）。

---

## Iter 3 — Layer1-3: batch_compile_kb --force / --changed-only

**Story**: batch_compile_kb.py 增加 --force 与 --changed-only 命令行开关
**Commit**: 2a1fd1c
**Files changed**:
- `APP/backend/scripts/batch_compile_kb.py` — 新增 `--force` / `--changed-only` argparse 开关 + `get_compiled_timestamps()` / `get_manifest_mtimes()` 辅助函数

**验证结果**:
```
conda run -n base python APP/backend/scripts/batch_compile_kb.py --help
→ 输出包含 --force 和 --changed-only ✅
```

**Blockers**: 无（运行环境需 conda base，系统 python3 无 requests 模块）。

---

## Iter 2 — Layer1-2: priority_topics 数据驱动

**Story**: priority_topics 从 KB/OUTPUT/converted/ 目录自动推导，不再硬编码
**Commit**: 8819a37
**Files changed**:
- `APP/backend/kb_compile_service.py` — 新增 `_CONVERTED_ROOT` 类变量 + `_default_priority_topics()` 方法；`compile_all()` 和 `lint()` 改用它替代硬编码列表

**验证结果**:
```
svc._default_priority_topics() → 18 domains
['APP', 'MDD开发框架', 'UI模板', 'YPD开发框架', 'bip-workflow', 'kingdee-workflow',
 '业务流', '云平台', '元数据', '公式', '导入导出', '打印', '权限', '流程中心', '消息', '组织', '规则', '配置迁移']
PASS: >= 17 domains, all required domains present ✅
```

**Blockers**: 无。

---

## Iter 4 — Integration: 全量灌库验证 + kb_hybrid_index batch commit 修复

**Story**: 全量灌库：POST /api/kb/sync 后 chunks 表出现 kb_local 且全部 17 个域 chunks > 0
**Commit**: (本次提交)
**Files changed**:
- `APP/backend/kb_hybrid_index.py` — 新增批量提交（每 50 条 commit）+ Chroma 异步刷新线程，防止 rebuild 崩溃时 DB 全清
- `docs/user-stories/kb-pipeline-fix.json` — 标记 passes: true

**验证结果**:
```
# 生产后端在 port 3000（单 worker，antigravity conda env）
# 需用 --noproxy '*' + 127.0.0.1 绕过 Surge 代理

curl --noproxy '*' -X POST http://127.0.0.1:3000/api/kb/sync
→ {"ok":true, "chunk_count":9359, "local_manifest_count":989}  ✅

sqlite3 data/sqlite/kb_chunks.db "SELECT source_kind,COUNT(*) FROM chunks GROUP BY source_kind;"
→ kb_local|6551 / apcom_docs|2808  ✅

# 17 域分布 SQL（含无前缀路径模式）
→ 18 个域全部 > 0:
  业务流(933) 流程中心(631) 规则(611) 配置迁移(552) 组织(463) 云平台(447)
  MDD(417) 打印(416) 公式(412) 元数据(394) YPD(247) 权限(246)
  消息(242) UI模板(225) 导入导出(188) bip-wf(82) kingdee(32) APP(11)  ✅

# FTS 验证（单词查询）
sqlite3 ... "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '公式';" → 660 ✅
```

**Blockers**:
- 后端端口是 3000 不是 8000，需绕过 Surge
- 原 rebuild 单事务提交：崩溃时 DB 全清→已修复为每 50 条批量 commit
- 原 Chroma 同步插入阻塞主线程→已改为后台 daemon thread 异步刷新
- `source_rel_path` 格式：新版 `流程中心/...`（无 KB/ 前缀），域分布 SQL 需同时匹配两种格式
