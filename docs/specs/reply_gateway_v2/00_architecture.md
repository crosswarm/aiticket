# 架构说明 — 智能回复网关 v2

Status: approved | Date: 2026-05-20

## Goal

将原来的"5道闸"短路模式重构为智能回复网关(reply_gateway)，5段网关**无条件全跑**，每段产出结构化 verdict + 展示卡片，网关结果统一附加到所有相关API响应。

## Non-goals

- 不重写 ChromaDB 检索层
- 不修改 supervisor LLM provider 选择
- 不修改 reply_cache 预生成逻辑

## 系统数据流

```
用户请求 → generate_reply_content
    ↓
ReplyGateway.run(issue_key, ai_analysis, ticket_meta)
    ├── G1 completeness  (completeness_checker)
    ├── G2 classification (chroma vote + routing rules + LLM)
    ├── G3 reuse         (reply_reuse_evaluator)
    ├── G4 specificity   (kb_evidence count → level)
    └── G5 supervisor    (reply_supervisor_agent)
    ↓
ReplyGatewayResult
    ├── gates: {G1..G5 verdicts}
    ├── display_cards: [UI 展示卡片]
    ├── final_action: normal_reply|return_to_support|handover|inquiry
    └── extra_operations: [{type, target, payload}]
    ↓
LLM 内容生成路径选择
    ↓
返回 dict (含 reply_gateway 字段)
    ↓
gate_decision_log.log_gate_decision (持久化)
```

## 依赖服务

| 服务 | 文件 | 用途 |
|------|------|------|
| completeness_checker | services/completeness_checker.py | G1 |
| classifier_service | services/classifier_service.py | G2 LLM分类 |
| vector_store | vector_store.py | G2 chroma投票 + G3 |
| reply_reuse_evaluator | services/reply_reuse_evaluator.py | G3 |
| reply_supervisor_agent | agents/reply_supervisor_agent.py | G5 |
| gate2_routing.json | data/gate2_routing.json | G2 路由规则 |
| gate_decision_log | services/gate_decision_log.py | 持久化 |

## 术语表

| 术语 | 定义 |
|------|------|
| verdict | pass/warn/fail/skipped 之一 |
| final_action | normal_reply/return_to_support/handover/inquiry |
| extra_operations | 附加自动化操作列表（移交/退回/通知） |
| display_cards | 前端展示卡片（每个 fail/warn gate 生成一张） |
| chroma_vote_ratio | 相似工单多数派项目占比 |
| composite_confidence | G5 分数的加权综合置信度（用于 auto_reply 决策） |
