# 单元测试用例 — 智能回复网关 v2

测试文件: `APP/backend/tests/test_reply_gateway.py`

## G1 completeness 用例

| 用例 ID | Gate | 场景 | 期望 |
|---------|------|------|------|
| UT-G1-01 | G1 | 完整工单 (description≥80字+复现步骤) | verdict=pass, missing_fields=[] |
| UT-G1-02 | G1 | description 空 | verdict=fail, inquiry_draft 非空 |
| UT-G1-03 | G1 | description 短但 attachments 有日志 | verdict=warn |

## G2 classification 用例

| 用例 ID | Gate | 场景 | 期望 |
|---------|------|------|------|
| UT-G2-01 | G2 | chroma 投异项目 5/5 + 规则命中 | verdict=fail, confidence≥0.85 |
| UT-G2-02 | G2 | chroma 投本项目 4/5 | verdict=pass |
| UT-G2-03 | G2 | 无规则命中 + chroma 分散 | verdict=warn |
| UT-G2-04 | G2 | vote=1.0, rule=1.0, llm=0 → 0.5+0.3+0=0.8 | confidence≈0.80 |

## G3 reuse 用例

| 用例 ID | Gate | 场景 | 期望 |
|---------|------|------|------|
| UT-G3-01 | G3 | composite≥0.85 | reuse_strategy=direct |
| UT-G3-02 | G3 | 0.55≤composite<0.85 | reuse_strategy=reference |
| UT-G3-03 | G3 | composite<0.55 | reuse_strategy=skip |

## G4 specificity 用例

| 用例 ID | Gate | 场景 | 期望 |
|---------|------|------|------|
| UT-G4-01 | G4 | KB evidence ≥3 + specific terms | level=high |
| UT-G4-02 | G4 | 无 KB 证据 | level=none |

## G5 supervisor 用例

| 用例 ID | Gate | 场景 | 期望 |
|---------|------|------|------|
| UT-G5-01 | G5 | normal reply + 无风险 | verdict=pass, score≥0.8 |
| UT-G5-02 | G5 | G1 fail 时传 inquiry_draft 当 generated_reply | G5 仍跑且能审计 |

## 依赖图 / 状态机用例

| 用例 ID | Gate | 场景 | 期望 |
|---------|------|------|------|
| UT-DEP-01 | 依赖 | G1 fail → G2/G3/G4=skipped, G5=pass | 状态机正确 |
| UT-DEP-02 | 依赖 | G2 fail (handover) → G3/G4/G5 全跑 | 不阻断 |

## final_action 聚合用例

| 用例 ID | Gate | 场景 | 期望 |
|---------|------|------|------|
| UT-FINAL-01 | 聚合 | G1=fail | final_action=return_to_support |
| UT-FINAL-02 | 聚合 | G1=pass, G2=fail (auto_move=true) | final_action=handover |
| UT-FINAL-03 | 聚合 | 全 pass | final_action=normal_reply |
