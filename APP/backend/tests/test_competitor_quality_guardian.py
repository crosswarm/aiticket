import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_quality_guardian_drops_irrelevant_marketing_or_wrong_domain_evidence():
    from competitor_quality_guardian import CompetitorQualityGuardian

    guardian = CompetitorQualityGuardian()
    requirement = {
        "title": "流程监控增加未来审批人的查询功能",
        "description": "需要在流程监控中查询未来审批人，并联动审批面板和流程图查看后续审批链路。",
    }
    bundle = {
        "scope": {
            "feature_intent": "future approver lookup",
            "must_have_terms": ["未来审批", "审批人", "代理审批"],
            "ui_targets": ["流程监控", "审批面板", "流程图"],
            "selected_vendors": ["SAP"],
        },
        "vendors": [
            {
                "vendor": "SAP",
                "implementation_summary": "支持 workflow substitution 与 substitute approver。",
                "citations": [
                    {
                        "title": "Create and Manage Substitution Rules - SAP Help Portal",
                        "url": "https://help.sap.com/docs/workflow-capability/workflow-cloud-foundry/create-and-manage-substitution-rules",
                        "source_type": "official",
                        "snippet": "Configure substitute approver and delegation rules in workflow administration.",
                    },
                    {
                        "title": "SAP Integrated Business Planning 2405",
                        "url": "https://help.sap.com/docs/supply-chain",
                        "source_type": "official",
                        "snippet": "Supply chain planning, transport and inventory monitoring.",
                    },
                ],
                "borrowable_patterns": ["代理审批规则配置"],
                "risks_or_limits": ["预测结果与实际审批链路可能存在差异。"],
            }
        ],
    }

    filtered, assessment = guardian.evaluate_bundle(bundle, requirement)

    assert assessment["decision"] == "passed"
    assert assessment["scores"]["relevance"] >= 4
    sap = filtered["vendors"][0]
    judgements = {item["title"]: item["judgement"] for item in sap["item_judgements"]}
    assert judgements["Create and Manage Substitution Rules - SAP Help Portal"] == "keep"
    assert judgements["SAP Integrated Business Planning 2405"] == "drop"
    assert len(sap["citations"]) == 1
    assert sap["feature_match"] is True


def test_quality_guardian_fails_bundle_when_only_low_value_homepage_style_evidence_exists():
    from competitor_quality_guardian import CompetitorQualityGuardian

    guardian = CompetitorQualityGuardian()
    requirement = {
        "title": "流程监控增加未来审批人的查询功能",
        "description": "需要在流程监控中查询未来审批人，并联动审批面板和流程图查看后续审批链路。",
    }
    bundle = {
        "scope": {
            "feature_intent": "future approver lookup",
            "must_have_terms": ["未来审批", "审批人"],
            "ui_targets": ["流程监控", "流程图"],
            "selected_vendors": ["致远"],
        },
        "vendors": [
            {
                "vendor": "致远",
                "implementation_summary": "提供数字化办公与协同运营平台。",
                "citations": [
                    {
                        "title": "致远互联官网-首页",
                        "url": "https://www.seeyon.com/",
                        "source_type": "official",
                        "snippet": "数字化办公、合同管理、人事管理、项目管理、知识管理、报表中心等全场景产品介绍。",
                    }
                ],
                "borrowable_patterns": [],
                "risks_or_limits": [],
            }
        ],
    }

    filtered, assessment = guardian.evaluate_bundle(bundle, requirement)

    assert assessment["decision"] == "failed"
    assert assessment["scores"]["reliability"] < 4 or assessment["scores"]["relevance"] < 4
    assert any("相关" in issue or "可靠" in issue for issue in assessment["blocking_issues"])
    assert filtered["vendors"][0]["citations"] == []


def test_quality_guardian_fails_draft_when_competitor_section_contains_marketing_noise():
    from competitor_quality_guardian import CompetitorQualityGuardian

    guardian = CompetitorQualityGuardian()
    artifact = {
        "title": "流程监控增加未来审批人的查询功能",
        "draft_content": """
## 竞品实现分析
SAP、金蝶、泛微、致远都很强大，覆盖财务、供应链、项目、人事、党建、文化建设、综合办公等大量场景。
滚动查看更多数字政府共性办公产品，解决方案覆盖所有企业数字化场景。
""".strip(),
        "competitor_comparison": {
            "vendors": [
                {
                    "vendor": "SAP",
                    "citations": [
                        {"title": "Workflow Substitution - SAP Help", "url": "https://help.sap.com/example", "source_type": "official"}
                    ],
                    "verification": {"status": "unverified", "captures": []},
                }
            ]
        },
        "quality_assessment": {
            "scores": {"richness": 4, "reliability": 4, "value": 4, "relevance": 4},
            "decision": "passed",
        },
    }

    assessment = guardian.evaluate_artifact(artifact)

    assert assessment["decision"] == "failed"
    assert assessment["scores"]["relevance"] < 4
    assert any("营销" in issue or "无关" in issue for issue in assessment["blocking_issues"])


def test_quality_guardian_warns_when_requirement_has_no_competitor_evidence_but_scope_is_relevant():
    from competitor_quality_guardian import CompetitorQualityGuardian

    guardian = CompetitorQualityGuardian()
    requirement = {
        "title": "重新提交单据时直接触发填写附言说明",
        "description": "希望在审批面板重新提交时必须填写附言说明，并把说明带给后续审批人。",
    }
    bundle = {
        "scope": {
            "feature_intent": "resubmit comment capture",
            "must_have_terms": ["重新提交", "附言", "说明"],
            "ui_targets": ["审批面板"],
            "selected_vendors": ["SAP", "金蝶"],
        },
        "vendors": [
            {
                "vendor": "SAP",
                "implementation_summary": "暂未检索到直接公开证据。",
                "citations": [],
                "borrowable_patterns": [],
                "risks_or_limits": [],
            },
            {
                "vendor": "金蝶",
                "implementation_summary": "暂未检索到直接公开证据。",
                "citations": [],
                "borrowable_patterns": [],
                "risks_or_limits": [],
            },
        ],
    }

    filtered, assessment = guardian.evaluate_bundle(bundle, requirement)

    assert assessment["decision"] == "warning"
    assert "代理审批" not in filtered["scope"]["must_have_terms"]
    assert "审批面板" in filtered["scope"]["ui_targets"]
    assert all(vendor["citations"] == [] for vendor in filtered["vendors"])


def test_quality_guardian_accepts_internal_dossier_evidence_with_verified_capture():
    from competitor_quality_guardian import CompetitorQualityGuardian

    guardian = CompetitorQualityGuardian()
    requirement = {
        "title": "审批面板重新提交时填写附言说明",
        "description": "希望在审批面板重新提交时必须填写附言说明，并把说明带给后续审批人。",
    }
    bundle = {
        "scope": {
            "feature_intent": "resubmit comment capture",
            "must_have_terms": ["重新提交", "附言", "说明"],
            "ui_targets": ["审批面板"],
            "selected_vendors": ["金蝶"],
        },
        "vendors": [
            {
                "vendor": "金蝶",
                "implementation_summary": "内部已沉淀审批面板补充说明截图。",
                "citations": [
                    {
                        "title": "金蝶审批面板补充说明截图",
                        "url": "file:///tmp/kingdee-approval-comment.png",
                        "source_type": "internal_dossier",
                        "snippet": "审批面板重新提交时展示补充说明并透传后续审批人。",
                    }
                ],
                "borrowable_patterns": ["重提时强制补充说明"],
                "risks_or_limits": ["需管理员先开启模板规则。"],
                "verification": {"status": "verified_authenticated", "captures": [{"title": "金蝶审批面板截图"}]},
            }
        ],
    }

    filtered, assessment = guardian.evaluate_bundle(bundle, requirement)

    assert assessment["decision"] == "passed"
    assert filtered["vendors"][0]["citations"][0]["source_type"] == "internal_dossier"
