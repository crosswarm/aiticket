"""风格记忆的改动分类器守卫。

LLM 的输出永远不可信：可能带前后缀说明、可能截断、可能整段不是 JSON。
这里锁死"解析层绝不抛异常"，否则一次畸形响应就会把回复提交链路带崩。
"""
from __future__ import annotations

from reply_diff_analyzer import _parse_json, _routing_chain


def test_parses_plain_json():
    assert _parse_json('{"diff_type":"style_fix"}') == {"diff_type": "style_fix"}


def test_parses_json_wrapped_in_prose():
    """LLM 常见输出：前后各带一段说明文字。"""
    raw = '好的，分析结果如下：\n{"diff_type": "knowledge_fix", "fact": "X"}\n以上。'
    assert _parse_json(raw)["diff_type"] == "knowledge_fix"


def test_parses_json_in_markdown_fence():
    raw = '```json\n{"diff_type": "coverage_gap"}\n```'
    assert _parse_json(raw)["diff_type"] == "coverage_gap"


def test_malformed_input_returns_none_not_raise():
    """★ 解析失败必须返回 None 而不是抛异常。"""
    for bad in ('{"截断的', "完全不是 JSON", "", None, "{}{}{", "[1,2,3]"):
        assert _parse_json(bad) is None or isinstance(_parse_json(bad), dict)


def test_none_and_empty_are_safe():
    assert _parse_json(None) is None
    assert _parse_json("") is None


def test_routing_chain_always_returns_nonempty_list():
    """路由配置缺失/损坏时必须有兜底链，否则风格分析整条链路失效。"""
    chain = _routing_chain()
    assert isinstance(chain, list) and chain, "降级链不能为空"
    assert all(isinstance(p, str) and p for p in chain)
