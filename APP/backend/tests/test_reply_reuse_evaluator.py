"""G3 历史复用评估器的行为守卫。

覆盖三处本会话真实踩过或高风险的逻辑：
1. 负配对加载 + mtime 哨兵热重载 —— badcase 上报后必须"下次刷新立即生效"。
2. `query_embedding` 为 None 的记录要被跳过（不能污染软压制），
   但硬屏蔽仍应据 wrong_ticket 生效。
3. `_assign_tier` 的相似度地板：sim 不够时无论 composite 多高都必须 skip，
   这是防止"高分但话题无关"的关键闸门。
"""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def rre(tmp_path, monkeypatch):
    """把负配对文件重定向到临时目录，避免碰真实数据。"""
    import services.reply_reuse_evaluator as mod
    importlib.reload(mod)
    p = tmp_path / "badcase_negative_pairs.jsonl"
    monkeypatch.setattr(mod, "_NEGATIVE_PAIRS_PATH", p, raising=True)
    monkeypatch.setattr(mod, "_neg_pairs_cache", {}, raising=False)
    monkeypatch.setattr(mod, "_neg_pairs_mtime", 0.0, raising=False)
    return mod, p


def _write(path, records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def test_missing_file_yields_empty_mapping(rre):
    mod, _ = rre
    assert mod._load_negative_pairs() == {}


def test_loads_pairs_keyed_by_wrong_ticket(rre):
    mod, p = rre
    _write(p, [
        {"wrong_ticket": "ABC-1", "query_issue_key": "ABC-9", "query_embedding": [0.1, 0.2]},
        {"wrong_ticket": "ABC-1", "query_issue_key": "ABC-8", "query_embedding": [0.3, 0.4]},
        {"wrong_ticket": "ABC-2", "query_issue_key": "ABC-7", "query_embedding": [0.5, 0.6]},
    ])
    pairs = mod._load_negative_pairs()
    assert set(pairs) == {"ABC-1", "ABC-2"}
    assert len(pairs["ABC-1"]) == 2


def test_records_without_embedding_are_skipped(rre):
    """★ 嵌入失败时我们仍会写行（query_embedding=None）以保住硬屏蔽，
    但软压制映射里不能混进空向量，否则余弦计算会炸。"""
    mod, p = rre
    _write(p, [
        {"wrong_ticket": "ABC-1", "query_issue_key": "ABC-9", "query_embedding": None},
        {"wrong_ticket": "ABC-2", "query_issue_key": "ABC-8", "query_embedding": [0.1, 0.2]},
    ])
    pairs = mod._load_negative_pairs()
    assert "ABC-1" not in pairs
    assert "ABC-2" in pairs


def test_malformed_line_does_not_break_loading(rre):
    mod, p = rre
    p.write_text('{"bad json\n{"wrong_ticket":"ABC-2","query_embedding":[0.1]}\n', encoding="utf-8")
    assert "ABC-2" in mod._load_negative_pairs()


def test_mtime_sentinel_triggers_reload(rre):
    """badcase 上报后端点会把 _neg_pairs_mtime 置 -1，必须导致重新读盘。"""
    mod, p = rre
    _write(p, [{"wrong_ticket": "ABC-1", "query_embedding": [0.1]}])
    assert set(mod._load_negative_pairs()) == {"ABC-1"}

    _write(p, [
        {"wrong_ticket": "ABC-1", "query_embedding": [0.1]},
        {"wrong_ticket": "ABC-2", "query_embedding": [0.2]},
    ])
    mod._neg_pairs_mtime = -1.0          # 端点写完后做的事
    assert set(mod._load_negative_pairs()) == {"ABC-1", "ABC-2"}


def test_embed_text_returns_none_instead_of_raising(rre, monkeypatch):
    """嵌入模型缺失/失败必须返回 None，绝不能抛异常——
    否则 badcase 上报的整段负配对写入会被 except 吞掉，连硬屏蔽都落不了库。"""
    mod, _ = rre
    monkeypatch.setattr(mod, "_ef_singleton", None, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "chromadb.utils", None)
    assert mod._embed_text("随便什么文本") is None


def test_cosine_basic_properties(rre):
    mod, _ = rre
    assert mod._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert mod._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert mod._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0     # 零向量不能除零崩溃


@pytest.mark.parametrize("sim,expected", [(0.60, "skip"), (0.75, "llm_blend"), (0.90, "direct")])
def test_assign_tier_similarity_floor(rre, sim, expected):
    """★ 相似度地板：composite 再高，sim 不够也必须 skip。
    这是"高质量但话题无关"被误判为可复用的防线。"""
    mod, _ = rre
    tier = mod._assign_tier(
        composite=0.95, example={"adopted": True},
        composite_threshold=0.80, llm_blend_min=0.60, sim=sim,
    )
    assert tier == expected


def test_assign_tier_requires_adopted_for_direct(rre):
    """未被采纳过的范例不能直接复用，只能进 llm_blend。"""
    mod, _ = rre
    assert mod._assign_tier(0.95, {"adopted": False}, 0.80, 0.60, sim=0.95) == "llm_blend"
