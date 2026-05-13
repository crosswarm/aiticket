import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_competitor_research_prefers_agent_reach_results():
    from competitor_research_service import CompetitorResearchService

    def fake_runner(args, capture_output, text, timeout, check):
        assert args[:3] == ["mcporter", "call", "exa.web_search_exa"]
        return FakeCompletedProcess(
            """
            {
              "results": [
                {
                  "title": "SAP Help - Workflow Substitution",
                  "url": "https://help.sap.com/docs/workflow-substitution",
                  "snippet": "Support substitute approver and workflow delegation."
                }
              ]
            }
            """.strip()
        )

    def fake_fetcher(url):
        return '<html><head><meta property="og:image" content="https://help.sap.com/image.png"></head></html>'

    service = CompetitorResearchService(command_runner=fake_runner, page_fetcher=fake_fetcher)
    result = service.research(
        {
            "title": "流程监控增加未来审批人的查询功能",
            "description": "需要参考主流厂商在未来审批与代理审批方面的实现。",
        },
        analysis_packet={"topic_names": ["未来审批流", "审批面板"]},
        top_k=1,
    )

    sap = next(item for item in result["vendors"] if item["vendor"] == "SAP")
    assert sap["citations"]
    assert sap["citations"][0]["url"] == "https://help.sap.com/docs/workflow-substitution"
    assert sap["key_capabilities"]
    assert sap["architecture_components"]
    assert sap["key_images"]
    assert sap["key_images"][0]["image_url"] == "https://help.sap.com/image.png"
    assert sap["screenshot_targets"]
    assert "替代审批人" in sap["implementation_summary"] or "substitute" in sap["implementation_summary"].lower()
    assert result["recommended_patterns"]
    assert result["architecture_overview"]["flow"]
    assert "借鉴" in result["summary"]


def test_competitor_research_parses_exa_plain_text_blocks():
    from competitor_research_service import CompetitorResearchService

    def fake_runner(args, capture_output, text, timeout, check):
        return FakeCompletedProcess(
            """Title: Set up approver substitution on behalf of the user - SAP Community
Author: Expert
URL: https://community.sap.com/t5/enterprise-resource-planning-q-a/set-up-approver-substitution-on-behalf-of-the-user/qaq-p/12734119
Text: Solved: Set up approver substitution on behalf of the user - SAP Community Workflow Administration app allows you to configure a substitute for an absent user.
"""
        )

    def fake_fetcher(url):
        return '<html><body><img src="/hero.jpg" alt="workflow hero"></body></html>'

    service = CompetitorResearchService(command_runner=fake_runner, page_fetcher=fake_fetcher)
    result = service.research(
        {
            "title": "流程监控增加未来审批人的查询功能",
            "description": "需要参考主流厂商在未来审批、代理审批、审批委托方面的实现方案。",
        },
        analysis_packet={"topic_names": ["未来审批流"]},
        top_k=1,
    )

    sap = next(item for item in result["vendors"] if item["vendor"] == "SAP")
    assert sap["citations"]
    assert sap["citations"][0]["url"].startswith("https://community.sap.com/")
    assert "替代审批人配置" in sap["implementation_summary"]
    assert sap["ui_touchpoints"]
    assert sap["screenshot_targets"][0]["url"].startswith("https://community.sap.com/")
    assert sap["key_images"][0]["image_url"].endswith("/hero.jpg")


def test_competitor_research_ranks_workflow_hits_above_irrelevant_docs():
    from competitor_research_service import CompetitorResearchService, VENDOR_SOURCES

    service = CompetitorResearchService(command_runner=lambda *args, **kwargs: None)
    hits = [
        {
            "title": "SAP Integrated Business Planning 2405",
            "url": "https://help.sap.com/docs/supply-chain",
            "snippet": "Supply chain planning, transport and inventory monitoring.",
        },
        {
            "title": "Set up approver substitution on behalf of the user - SAP Community",
            "url": "https://community.sap.com/workflow-substitution",
            "snippet": "Workflow Administration app allows you to configure a substitute approver and delegation.",
        },
    ]

    ranked = service._rank_hits(hits, VENDOR_SOURCES["SAP"], ["未来审批流", "代理审批"], top_k=1)
    assert len(ranked) == 1
    assert ranked[0]["url"] == "https://community.sap.com/workflow-substitution"


def test_competitor_research_extracts_key_images_from_article_html():
    from competitor_research_service import CompetitorResearchService

    service = CompetitorResearchService(command_runner=lambda *args, **kwargs: None)
    images = service._extract_image_candidates(
        "https://example.com/article",
        """
        <html>
          <head>
            <meta property="og:image" content="https://cdn.example.com/cover.png" />
          </head>
          <body>
            <img src="/content-1.jpg" alt="流程图示" />
            <img src="/logo.svg" alt="logo" />
          </body>
        </html>
        """,
    )
    assert images[0]["image_url"] == "https://cdn.example.com/cover.png"
    assert any(item["image_url"] == "https://example.com/content-1.jpg" for item in images)
    assert all(not item["image_url"].endswith(".svg") for item in images)


def test_competitor_research_builds_resubmit_comment_scope_for_t001_like_requirements():
    from competitor_research_service import CompetitorResearchService

    service = CompetitorResearchService(command_runner=lambda *args, **kwargs: None)

    scope = service._build_scope(
        {
            "title": "用户需要在重新提交单据时直接触发填写附言说明",
            "description": "目前审批面板上已经有附言功能，希望在重新提交时直接触发填写附言说明，并把说明带给后续审批人。",
        },
        {"topic_names": ["审批面板", "重新提交"]},
    )

    assert scope["feature_intent"] == "resubmit comment capture"
    assert "重新提交" in scope["must_have_terms"]
    assert "附言" in scope["must_have_terms"]
    assert "说明" in scope["must_have_terms"]
    assert "审批人" not in scope["must_have_terms"]
    assert "审批面板" in scope["ui_targets"]


def test_competitor_research_uses_intent_specific_queries_for_resubmit_comment_scope():
    from competitor_research_service import CompetitorResearchService

    captured_queries = []

    def fake_runner(args, capture_output, text, timeout, check):
        captured_queries.append(args[3])
        return FakeCompletedProcess('{"results": []}')

    service = CompetitorResearchService(command_runner=fake_runner)
    service.research(
        {
            "title": "用户需要在重新提交单据时直接触发填写附言说明",
            "description": "希望在审批面板重新提交时填写附言说明，并把说明带给后续审批人。",
        },
        analysis_packet={"topic_names": ["审批面板", "重新提交"]},
        top_k=1,
    )

    joined = " ".join(captured_queries)
    assert "重新提交" in joined
    assert "附言" in joined or "说明" in joined
    assert "workflow substitution approver" not in joined
    assert "审批委托 代理审批 工作流" not in joined
