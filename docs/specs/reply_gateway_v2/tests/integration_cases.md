# 集成测试用例 — 智能回复网关 v2

测试文件: `APP/backend/tests/test_reply_gateway_api.py`

## API 端点集成用例

| 用例 ID | API | 场景 | 期望 |
|---------|-----|------|------|
| IT-API-01 | POST /api/board/generate-reply | 任意工单 | 响应含 reply_gateway.gates.G1-G5 |
| IT-API-02 | POST /api/reply/generate-by-module | 模块回复 | 响应含 reply_gateway |
| IT-API-03 | POST /api/reply/refine | refine 后 | reply_gateway.gates.G4/G5 已更新 |
| IT-API-04 | POST /api/board/check-completeness | only=G1 | 仅返回 G1 verdict |
| IT-API-05 | GET /api/board | 列表 | 每个工单含 gate_summary |
| IT-API-06 | GET /api/board/issues | 同上 | 同上 |
| IT-API-07 | GET /api/board/issue-detail/{key} | 工单详情 | 完整 reply_gateway |
| IT-API-08 | POST /api/board/precompute-replies | 批量预热 | gate_decision_log 写入新记录 |
| IT-API-09 | GET /api/board/gate-log/{key} | 新增端点 | 返回历史 verdict |

## 缓存集成用例

| 用例 ID | API | 场景 | 期望 |
|---------|-----|------|------|
| IT-CACHE-01 | reply_cache 命中 + gate_log 有数据 | 二次请求 | reply_gateway 从 gate_log LRU 取 |
| IT-CACHE-02 | 预生成钩子 | 工单分析完成后 | gate_decision_log 自动追加一条 |

## 智能决策集成用例

| 用例 ID | API | 场景 | 期望 |
|---------|-----|------|------|
| IT-DECIDE-01 | auto_reply_decider | gates 全 pass + composite≥0.95 | action=auto_replied_low_risk |
| IT-DECIDE-02 | auto_reply_decider | G2=fail | blocked_by=[G2], action=needs_decision |
