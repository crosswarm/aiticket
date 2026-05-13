import json
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_design_fact_service_merges_profiles_kb_and_manual_packet(tmp_path):
    from design_fact_service import DesignFactService

    kb_root = tmp_path / "KB"
    profile_dir = kb_root / "REQ_POOL" / "product_design_profiles"
    dossier_dir = tmp_path / "runtime_dossiers"
    profile_dir.mkdir(parents=True)
    dossier_dir.mkdir(parents=True)

    (profile_dir / "approval-comment.json").write_text(
        json.dumps(
            {
                "feature_key": "approval_comment_resubmit",
                "title": "审批面板重提补充说明",
                "aliases": ["审批面板", "重新提交", "附言说明", "补充说明"],
                "module": "流程中心",
                "design_principles": ["补充说明应服务于审批上下文连续性。"],
                "process_level_rules": [
                    {"name": "流程定义支持补充说明", "summary": "流程设计阶段可声明是否启用重提说明。"}
                ],
                "step_level_rules": [
                    {"name": "审批面板显示说明", "summary": "审批面板需要展示补充说明与历史意见。"}
                ],
                "tenant_level_params": [],
                "document_flow_properties": [
                    {"name": "单据属性透传", "summary": "说明透传依赖单据流程属性映射。"}
                ],
                "source_refs": ["[PROFILE] approval_comment_resubmit"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (dossier_dir / "kingdee-approval-comment.json").write_text(
        json.dumps(
            {
                "vendor": "金蝶",
                "feature_key": "approval_comment_resubmit",
                "feature_summary": "支持在审批节点查看补充说明。",
                "supported": True,
                "ui_touchpoints": ["审批面板"],
                "config_levels": ["环节级"],
                "constraints": ["需管理员预先开启对应模板规则。"],
                "evidence_items": [{"title": "内部截图", "url": "file:///captures/kingdee.png", "source_level": "internal_dossier"}],
                "verification_status": "verified_authenticated",
                "captures": [{"title": "金蝶审批面板截图"}],
                "updated_at": "2026-03-21T12:00:00",
                "aliases": ["审批面板", "附言说明", "重新提交"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    class FakeKBRuntimeService:
        def search_bundle(self, query, top_k=8, source_kind=None, category=None):
            return {
                "items": [
                    {
                        "name": "特殊场景-待办打开单据的展示",
                        "summary": "审批面板展示说明信息和单据上下文。",
                        "source_kind": "kb_local",
                        "source_rel_path": "流程中心/扩展和客开/10. 特殊场景-待办打开单据的展示.docx",
                        "citation_label": "[KB] 特殊场景-待办打开单据的展示",
                        "weighted_rank_score": 92,
                    },
                    {
                        "name": "流程流转-单据状态对照表",
                        "summary": "单据状态与流程属性的对照关系。",
                        "source_kind": "kb_local",
                        "source_rel_path": "流程中心/扩展和客开/6. 流程流转-单据状态对照表.docx",
                        "citation_label": "[KB] 流程流转-单据状态对照表",
                        "weighted_rank_score": 88,
                    },
                ],
                "primary_materials": [],
            }

    service = DesignFactService(
        kb_runtime_service=FakeKBRuntimeService(),
        kb_root=str(kb_root),
        runtime_dossier_dir=str(dossier_dir),
    )
    requirement = {
        "req_id": "REQ-MYPROJECT-T001",
        "title": "审批面板重新提交时填写附言说明",
        "description": "希望在审批面板重新提交时必须填写附言说明，并把说明带给后续审批人。",
        "requirement_fact_packet": {
            "surface": "审批面板",
            "trigger_action": "重新提交",
            "input_object": "附言说明",
            "requiredness": "重新提交时必填",
            "visibility_scope": "后续审批人可见",
            "persistence_scope": "随审批意见留痕并透传后续节点",
            "config_level": ["环节级", "单据属性级"],
            "related_process_types": ["审批流程"],
            "related_bill_types": ["请假单"],
            "known_constraints": ["移动端是否展示待确认"],
            "manual_notes": "客户明确要求重提时补充说明。",
            "reference_links": ["https://internal.example.com/design"],
            "attachments": [],
        },
    }

    context = service.build_requirement_context(requirement)

    assert context["design_fact_bundle"]["coverage_summary"]["step_rule_count"] >= 1
    assert context["design_fact_bundle"]["coverage_summary"]["document_property_count"] >= 1
    assert context["design_fact_bundle"]["coverage_summary"]["manual_fact_count"] >= 1
    assert "租户级参数仍不明确" in "；".join(context["design_fact_bundle"]["missing_facts"])
    assert context["competitor_dossiers"][0]["vendor"] == "金蝶"
    assert context["competitor_dossiers"][0]["verification_status"] == "verified_authenticated"
