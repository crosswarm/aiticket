"""build_issue_query 行为守卫。

为什么这 47 行值得单独测：badcase 负配对的 query_embedding 就是嵌它的产物。
本会话实测踩过——当查询正文为空时，写进 badcase_negative_pairs.jsonl 的
query_embedding 是 None，**软压制直接失效**（只剩硬屏蔽）。
所以"什么时候会返回空"必须被钉死。
"""
from __future__ import annotations

from services.query_builder import build_issue_query


def test_uses_ai_analysis_fields_when_present():
    q = build_issue_query("ABC-1", {"issue_title": "审批流超时", "issue_description": "跨组织未触发"})
    assert "审批流超时" in q and "跨组织未触发" in q


def test_falls_back_to_cache_when_ai_fields_empty():
    """ai_analysis 没有正文时要回退到 Jira 缓存，而不是直接放弃。"""
    def cache_fn(key):
        assert key == "ABC-2"
        return {"summary": "缓存里的标题", "description": "缓存里的描述"}

    q = build_issue_query("ABC-2", {}, cache_fn=cache_fn)
    assert "缓存里的标题" in q
    assert "缓存里的描述" in q


def test_cache_fallback_accepts_object_not_only_dict():
    """cache_fn 可能返回 JiraIssue 对象而非 dict，两种都得吃得下。"""
    class _Issue:
        summary = "对象标题"
        description = "对象描述"

    q = build_issue_query("ABC-3", None, cache_fn=lambda k: _Issue())
    assert "对象标题" in q


def test_returns_empty_when_nothing_available():
    """★ 这是 badcase 软压制失效的根因：全空时返回 ""，
    调用方必须据此判断并跳过嵌入，而不是拿空串去 embed。"""
    assert build_issue_query("ABC-4", {}) == ""
    assert build_issue_query("ABC-5", None, cache_fn=lambda k: None) == ""


def test_cache_fn_exception_does_not_propagate():
    """缓存查询失败不能让整条上报链路挂掉。"""
    def boom(_):
        raise RuntimeError("cache down")

    assert build_issue_query("ABC-6", {}, cache_fn=boom) == ""


def test_respects_max_len():
    long_desc = "很长" * 5000
    q = build_issue_query("ABC-7", {"issue_title": "标题", "issue_description": long_desc}, max_len=300)
    assert len(q) <= 300
