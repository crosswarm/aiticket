# 数据契约 — reply_gateway JSON Schema

Status: approved | Date: 2026-05-20

## reply_gateway 结构

```json
{
  "version": "v2",
  "gates": {
    "G1_completeness": {
      "verdict": "pass|warn|fail|skipped",
      "score": 0.0,
      "missing_fields": [],
      "insufficient_type": "",
      "inquiry_draft": "",
      "rule_matched": ""
    },
    "G2_classification": {
      "verdict": "pass|warn|fail|skipped",
      "confidence": 0.0,
      "ticket_project_from_key": "MYPROJECT",
      "predicted_project_from_chroma": "OTHERPROJECT",
      "predicted_module_from_llm": "客户化开发",
      "final_project_decision": "OTHERPROJECT",
      "transfer_to": "OTHERPROJECT",
      "transferee": "user001",
      "transferee_display": "用户A",
      "domain_module": "客户化开发",
      "auto_move_eligible": true,
      "matched_rule_id": "kf_handover",
      "chroma_vote_ratio": 0.8,
      "rule_match_score": 0.9,
      "llm_match_score": 1.0
    },
    "G3_reuse": {
      "verdict": "pass|warn|fail|skipped",
      "composite_score": 0.0,
      "candidate_key": "MYPROJECT-12345",
      "candidate_summary": "",
      "reuse_strategy": "direct|reference|skip"
    },
    "G4_specificity": {
      "verdict": "pass|warn|fail|skipped",
      "level": "high|medium|low|none",
      "kb_evidence_count": 0,
      "weak_points": []
    },
    "G5_supervisor": {
      "verdict": "pass|warn|fail|skipped",
      "score": 0.0,
      "risk_flags": [],
      "step_safety": "",
      "rationale": "",
      "provider_used": ""
    }
  },
  "display_cards": [
    {
      "gate": "G2_classification",
      "title": "项目分类",
      "badge_color": "red|amber|green|gray",
      "badge_text": "建议移交 OTHERPROJECT",
      "message": "工单号前缀 MYPROJECT 但相似工单多在 OTHERPROJECT（5/5）",
      "action_hint": "建议移交至对应项目"
    }
  ],
  "final_action": "normal_reply|return_to_support|handover|inquiry",
  "extra_operations": [
    {
      "type": "move_jira|return_to_support|notify",
      "target": "OTHERPROJECT",
      "payload": {}
    }
  ],
  "auto_decision": {
    "composite_confidence": 0.0,
    "threshold_hit": "auto_normal|needs_decision|veto",
    "action": "auto_replied_normal|auto_replied_low_risk|needs_decision|veto",
    "decided_by": "auto_reply_decider",
    "blocked_by": []
  }
}
```

## verdict 含义

| verdict | 含义 |
|---------|------|
| pass | 检查通过，无需干预 |
| warn | 通过但有提醒，展示提示卡片 |
| fail | 检查失败，触发额外操作（追问/移交等） |
| skipped | 前置依赖缺失，本 gate 无法执行 |

## G2 置信度计算

```
confidence = 0.5 × chroma_vote_ratio
           + 0.3 × rule_match_score
           + 0.2 × llm_match_score
```

- `chroma_vote_ratio`: 相似工单 top_k 中多数派项目占比
- `rule_match_score`: 路由规则关键词命中率（0-1）
- `llm_match_score`: LLM 预测模块与规则 domain_module 一致性（0 或 1）
