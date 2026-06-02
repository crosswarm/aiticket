# 测试计划 — 智能回复网关 v2

## 测试文件位置

- 单元测试: `APP/backend/tests/test_reply_gateway.py`
- 集成测试: `APP/backend/tests/test_reply_gateway_api.py`
- E2E: `APP/backend/tests/e2e_reply_gateway.sh`
- 测试夹具: `docs/specs/reply_gateway_v2/tests/fixtures/`

## 运行命令

```bash
cd APP/backend
python -m pytest tests/test_reply_gateway.py -v
python -m pytest tests/test_reply_gateway_api.py -v
```

## 覆盖目标

- 单元测试覆盖率 ≥ 80%
- 所有 UT-* 用例通过
- 所有 IT-API-* 用例通过

## 测试夹具文件

`docs/specs/reply_gateway_v2/tests/fixtures/` 下需要的样本工单（脱敏）：

| 文件 | 场景 |
|------|------|
| `complete_ticket.json` | 完整工单（G1 pass） |
| `incomplete_ticket.json` | 信息不足（G1 fail） |
| `handover_ticket.json` | 需移交工单（G2 fail handover） |
| `yonsuite_ticket.json` | 公有云工单（G2 路由排除） |
| `high_reuse_ticket.json` | 历史复用 direct |
| `low_specificity_ticket.json` | KB 证据弱 |

## 回归/E2E 测试

| 用例 ID | 场景 | 命令 | 期望 |
|---------|------|------|------|
| E2E-01 | 启用 G2 后工单分类 | `curl POST /api/board/generate-reply` | gates.G2.verdict 有值 |
| E2E-02 | board.html 看板模式 | 浏览器渲染 + 截图 | 每卡可见 5 mini-badge |
| E2E-03 | precompute-replies 全量跑 | 跑 100 条 | gate_decision_log 100 条 |
