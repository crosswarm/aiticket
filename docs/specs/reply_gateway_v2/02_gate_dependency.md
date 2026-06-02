# G1-G5 依赖图与 final_action 状态机

Status: approved | Date: 2026-05-20

## 依赖图

```
G1_completeness
  ├── verdict=fail  → G2=skipped, G3=skipped, G4=skipped
  │                   G5=run (审计 inquiry_draft 质量)
  │                   final_action = return_to_support
  │
  └── verdict=pass/warn
        G2_classification
          ├── verdict=fail  → G3=run, G4=run, G5=run
          │                   final_action = handover
          │
          └── verdict=pass/warn
                G3_reuse (run)
                G4_specificity (run)
                G5_supervisor (run)
                final_action = normal_reply
```

## final_action 决策逻辑

1. G1 fail → `return_to_support`
2. G1 pass + G2 fail → `handover`
3. G1 pass + G2 pass/warn → `normal_reply`
4. G1 pass + G2 pass/warn + 额外 inquiry 信号 → `inquiry`（保留扩展）

## extra_operations 生成逻辑

| 条件 | extra_operations 条目 |
|------|----------------------|
| G1 fail | `{type: "return_to_support", target: issue_key}` |
| G2 fail, auto_move_eligible=true | `{type: "move_jira", target: G2.transfer_to, payload: {transferee, domain_module}}` |
| G2 fail, auto_move_eligible=false | `{type: "notify", target: G2.transferee, payload: {message: "建议移交"}}` |

## display_cards 生成逻辑

每个 verdict 为 fail 或 warn 的 gate 生成一张展示卡片：

| Gate | fail badge | warn badge |
|------|-----------|------------|
| G1 | red "信息不足" | amber "信息较少" |
| G2 | red "建议移交 {project}" | amber "疑似错分" |
| G3 | — | amber "历史相似低" |
| G4 | red "KB证据不足" | amber "证据较少" |
| G5 | red "监督风险" | amber "需人工审阅" |
