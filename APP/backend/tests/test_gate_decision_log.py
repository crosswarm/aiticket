"""五道闸决策日志的行为守卫。

关注点：采纳分层的阈值语义（它直接决定"风格记忆"学到什么），
以及 JSONL 追加/轮转这类会在 172 长跑中出问题的机制。
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def gdl(tmp_path, monkeypatch):
    import services.gate_decision_log as mod
    importlib.reload(mod)
    p = tmp_path / "gate_decisions.jsonl"
    monkeypatch.setattr(mod, "_JSONL_PATH", str(p), raising=True)
    monkeypatch.setattr(mod, "_JSONL_PATH_obj", p, raising=True)
    monkeypatch.setattr(mod, "_debounce", {}, raising=True)
    return mod, p


def test_text_sim_identical_and_disjoint(gdl):
    mod, _ = gdl
    assert mod._text_sim("完全一样的回复", "完全一样的回复") == pytest.approx(1.0)
    assert mod._text_sim("", "") == 1.0          # 两个空串视为相同
    assert mod._text_sim("有内容", "") == 0.0     # 一空一非空视为完全不同


@pytest.mark.parametrize("sim,expected", [(0.99, "direct"), (0.85, "direct"), (0.60, "partial"), (0.10, "none")])
def test_sim_tier_thresholds(gdl, sim, expected):
    """采纳分层：几乎没改=direct，改了一部分=partial，推翻重写=none。
    这三档就是 UI 上「直接采纳 / 加工处理 / 未采纳」的来源。"""
    mod, _ = gdl
    assert mod._sim_tier(sim) == expected


def test_sim_tier_is_monotonic(gdl):
    """相似度越高，采纳层级不应该反而变低。"""
    mod, _ = gdl
    order = {"none": 0, "partial": 1, "direct": 2}
    tiers = [order[mod._sim_tier(s / 100)] for s in range(0, 101, 5)]
    assert tiers == sorted(tiers)


def test_rotation_triggers_past_size_limit(gdl, monkeypatch):
    """长跑机器上 JSONL 会持续增长，必须能轮转，否则读取会越来越慢。"""
    mod, p = gdl
    p.write_text("x" * 200, encoding="utf-8")
    monkeypatch.setattr(mod, "_JSONL_MAX_BYTES", 100, raising=True)
    mod._maybe_rotate()
    assert not p.exists() or p.stat().st_size == 0
    assert list(p.parent.glob("gate_decisions.*.jsonl")), "应生成轮转后的历史文件"


def test_rotation_noop_below_limit(gdl, monkeypatch):
    mod, p = gdl
    p.write_text("小文件", encoding="utf-8")
    monkeypatch.setattr(mod, "_JSONL_MAX_BYTES", 10 * 1024 * 1024, raising=True)
    mod._maybe_rotate()
    assert p.exists() and p.read_text(encoding="utf-8") == "小文件"


def test_get_recent_decision_missing_returns_none(gdl):
    mod, _ = gdl
    assert mod.get_recent_decision("NOPE-1") is None
