"""
测试 analysis_cache quality gate 逻辑。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from board_service_chroma import (
    _cache_quality_gate,
    _reply_cache_block_reason,
    _supervisor_direct_failed,
)


class TestCacheQualityGate:
    def test_new_entry_always_passes(self):
        """全新 entry（existing 为空）应直接通过"""
        entry = {"issue_title": "test", "confidence": 0.9}
        allow, reason, merged = _cache_quality_gate("LCZX-1", entry, {})
        assert allow is True
        assert reason == "new_entry"
        assert merged["issue_title"] == "test"

    def test_title_preserved_when_new_empty(self):
        """新 entry title 为空但已有非空 title → 应保留旧 title"""
        existing = {"issue_title": "原始标题", "issue_description": "原始描述", "confidence": 0.8}
        new_entry = {"issue_title": "", "confidence": 0.5}
        allow, reason, merged = _cache_quality_gate("LCZX-2", new_entry, existing)
        assert allow is True
        assert reason == "title_preserved"
        assert merged["issue_title"] == "原始标题"
        assert merged["issue_description"] == "原始描述"
        assert merged.get("_preserved_from_regression") is True

    def test_normal_update_passes(self):
        """新旧都有 title → normal 通过"""
        existing = {"issue_title": "旧标题", "confidence": 0.7}
        new_entry = {"issue_title": "新标题", "confidence": 0.9}
        allow, reason, merged = _cache_quality_gate("LCZX-3", new_entry, existing)
        assert allow is True
        assert reason == "normal"
        assert merged["issue_title"] == "新标题"

    def test_both_empty_title_passes(self):
        """新旧都没有 title → 允许（合法新工单，title 尚未可知）"""
        existing = {"confidence": 0.3}
        new_entry = {"confidence": 0.4}
        allow, reason, merged = _cache_quality_gate("LCZX-4", new_entry, existing)
        assert allow is True

    def test_new_entry_does_not_mutate_input(self):
        """quality gate 不应修改原始 new_entry dict（防副作用）"""
        existing = {"issue_title": "原标题"}
        new_entry = {"issue_title": "", "confidence": 0.5}
        original_id = id(new_entry)
        _, _, merged = _cache_quality_gate("LCZX-5", new_entry, existing)
        # merged 应是 new dict，new_entry 未被修改
        assert merged is not new_entry
        assert new_entry["issue_title"] == ""

    def test_description_fallback_from_existing(self):
        """新 entry 没有 description 且 title 需要保留时，description 也从 existing 取"""
        existing = {"issue_title": "原标题", "issue_description": "原描述"}
        new_entry = {"confidence": 0.5}
        _, reason, merged = _cache_quality_gate("LCZX-6", new_entry, existing)
        assert reason == "title_preserved"
        assert merged["issue_description"] == "原描述"

    # ── 降级保护（QCL 2026-06-11：无凭据强制重析洗掉好分析）──────────────

    def test_rule_engine_does_not_overwrite_llm_analysis(self):
        """rule_engine 降级产物不得整体覆盖已有 LLM 分析"""
        existing = {"issue_title": "原标题", "model_used": "deepseek-v4-flash",
                    "solution_recommendation": "检查打印模板字段绑定"}
        new_entry = {"issue_title": "原标题", "model_used": "rule_engine"}
        allow, reason, merged = _cache_quality_gate("YDY-9892", new_entry, existing)
        assert allow is True
        assert reason == "llm_analysis_preserved"
        assert merged["model_used"] == "deepseek-v4-flash"
        assert merged["solution_recommendation"] == "检查打印模板字段绑定"

    def test_rule_engine_overwrites_rule_engine(self):
        """旧的也是 rule_engine → 正常覆盖（无 LLM 内容可保护）"""
        existing = {"issue_title": "原标题", "model_used": "rule_engine"}
        new_entry = {"issue_title": "原标题", "model_used": "rule_engine", "confidence": 0.4}
        _, reason, merged = _cache_quality_gate("YDY-1", new_entry, existing)
        assert reason == "normal"
        assert merged["confidence"] == 0.4

    def test_empty_solution_field_preserved_from_llm_existing(self):
        """新 LLM 分析缺 solution 字段（如该 feature 被兜底关闭跳过）→ 字段级保留旧值"""
        existing = {"issue_title": "原标题", "model_used": "glm-5",
                    "solution_recommendation": "旧解决方案", "solution_suggestion": "旧建议"}
        new_entry = {"issue_title": "原标题", "model_used": "minimax", "confidence": 0.9}
        _, reason, merged = _cache_quality_gate("YDY-2", new_entry, existing)
        assert merged["solution_recommendation"] == "旧解决方案"
        assert merged["solution_suggestion"] == "旧建议"
        assert merged["model_used"] == "minimax"
        assert merged.get("_preserved_from_regression") is True

    def test_new_solution_field_wins_when_present(self):
        """新分析自带 solution → 不做保留，用新值"""
        existing = {"issue_title": "原标题", "model_used": "glm-5",
                    "solution_recommendation": "旧解决方案"}
        new_entry = {"issue_title": "原标题", "model_used": "minimax",
                     "solution_recommendation": "新解决方案"}
        _, _, merged = _cache_quality_gate("YDY-3", new_entry, existing)
        assert merged["solution_recommendation"] == "新解决方案"


class TestSupervisorDirectFailClosed:
    """G5 supervisor 在 Gate3 direct 出口的 fail-closed 判定"""

    class _Sup:
        def __init__(self, score):
            self.supervisor_score = score

    def test_none_result_fails(self):
        assert _supervisor_direct_failed(None) is True

    def test_none_score_fails_closed(self):
        """LLM 阻断/配额/解析失败 → score=None → 必须判失败（不放行 direct）"""
        assert _supervisor_direct_failed(self._Sup(None)) is True

    def test_low_score_fails(self):
        assert _supervisor_direct_failed(self._Sup(0.5)) is True

    def test_passing_score_ok(self):
        assert _supervisor_direct_failed(self._Sup(0.8)) is False


class TestReplyCacheQualityGate:
    def test_blocks_analysis_template_reply(self):
        reply = "【解决方案】\n建议产品经理检查文档。\n\n【功能影响】\n文档需求\n\n【推荐处理】 产品经理"
        assert _reply_cache_block_reason(reply) == "analysis_template"

    def test_blocks_no_evidence_rich_cache(self):
        entry = {
            "grounded_confidence": {"evidence_status": "no_evidence"},
            "reply_gateway": {
                "gates": {
                    "G4_specificity": {
                        "verdict": "fail",
                        "level": "none",
                        "kb_evidence_count": 0,
                    }
                }
            },
        }
        assert _reply_cache_block_reason("您好！已转达产品团队评估，谢谢", entry=entry) == "no_evidence"

    def test_allows_customer_facing_reply_with_evidence(self):
        entry = {
            "grounded_confidence": {"evidence_status": "kb_only"},
            "reply_gateway": {
                "gates": {
                    "G4_specificity": {
                        "verdict": "pass",
                        "level": "medium",
                        "kb_evidence_count": 1,
                    }
                }
            },
        }
        assert _reply_cache_block_reason("您好！该能力请按知识库路径配置后验证，谢谢", entry=entry) == ""


class TestForceAnalyzeUserIdPassthrough:
    """force_analyze（单工单强制重析）必须把触发用户透传给 AIWorker（用户级 LLM 凭据）"""

    def test_force_analyze_passes_user_id_to_worker(self):
        from unittest.mock import MagicMock
        from board_service_chroma import BoardService

        svc = BoardService.__new__(BoardService)
        svc.vector_store = MagicMock()
        svc.worker = MagicMock()
        svc.analysis_status = {}
        client = MagicMock()
        client.search_issues_rest_api.return_value = {"issues": [{}]}
        fake_issue = MagicMock()
        client.parse_search_response.return_value = [fake_issue]

        result = svc.force_analyze("YDY-9892", jira_client=client, user_id="u-123")

        assert result["status"] == "submitted"
        svc.worker.submit.assert_called_once_with(
            fake_issue, priority=0, skip_reuse=True, user_id="u-123")
