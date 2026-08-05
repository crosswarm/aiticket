"""G5 监督审计的解析守卫。

监督结果直接影响回复能否自动发出，所以"解析不了"必须退化成
明确的 llm_failed + score=None（让上游走人工），而不是伪造一个高分放行。
"""
from __future__ import annotations

from agents.reply_supervisor_agent import _parse_result


def test_parses_wellformed_result():
    raw = '{"supervisor_score":0.82,"risk_flags":["a"],"evidence_coverage":0.7,"step_safety":"safe","rationale":"ok"}'
    r = _parse_result(raw, "zhipu")
    assert r.status == "ok"
    assert r.supervisor_score == 0.82
    assert r.risk_flags == ["a"]
    assert r.provider_used == "zhipu"


def test_extracts_json_from_surrounding_prose():
    raw = '审计结论：\n{"supervisor_score": 0.4, "step_safety": "risky"}\n仅供参考'
    r = _parse_result(raw, "minimax")
    assert r.status == "ok"
    assert r.supervisor_score == 0.4


def test_no_json_degrades_to_failed_not_high_score():
    """★ 关键安全性质：解析不出来时不能给出可放行的分数。"""
    r = _parse_result("模型今天不想回答", "local")
    assert r.status == "llm_failed"
    assert r.supervisor_score is None


def test_broken_json_degrades_to_failed():
    r = _parse_result('{"supervisor_score": }', "local")
    assert r.status == "llm_failed"
    assert r.supervisor_score is None


def test_rationale_is_truncated():
    raw = '{"supervisor_score":0.5,"rationale":"%s"}' % ("很长" * 500)
    r = _parse_result(raw, "zhipu")
    assert len(r.rationale) <= 200


def test_missing_fields_get_defaults():
    r = _parse_result('{"supervisor_score": 0.6}', "zhipu")
    assert r.status == "ok"
    assert r.evidence_coverage == 0.5
    assert r.step_safety == "safe"
