"""检索分层加权的单元测试。

最关键的保证是**不过滤**：跨 label 的知识必须仍然召得回，只是排后面。
实测 LCZX 一个 Jira 项目的知识横跨 PF / OA / BMM 三个 label，
一旦退化成硬过滤，正确答案会被直接丢掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bip_taxonomy import (  # noqa: E402
    APPLICATION_BOOST,
    LABEL_BOOST,
    LEGACY_MODULE_BOOST,
    PROJECT_BOOST,
    BipTaxonomy,
    apply_taxonomy_boost,
)


@pytest.fixture(scope="module")
def taxonomy() -> BipTaxonomy:
    return BipTaxonomy()


def items():
    return [
        {"id": "same_label", "l1_module": "数字化建模", "l2_module": "工作流", "score": 0.5, "project_key": "LCZX"},
        {"id": "same_label_other_app", "l1_module": "数字化建模", "l2_module": "业务流", "score": 0.5, "project_key": "_global"},
        {"id": "cross_label", "l1_module": "业务模型管理", "l2_module": "打印模板", "score": 0.5, "project_key": "_global"},
        {"id": "junk", "l1_module": "_excluded", "l2_module": "", "score": 0.9, "project_key": "_global"},
    ]


def by_id(rows):
    return {r["id"]: r for r in rows}


# ---------------------------------------------------------------- 加权


def test_label_and_application_stack(taxonomy: BipTaxonomy):
    out = by_id(apply_taxonomy_boost(items(), taxonomy, module_boost=["流程中心"]))
    # 「流程中心」→ 工作流 / 数字化建模，label 与 application 都命中
    assert out["same_label"]["score"] == pytest.approx(0.5 + LABEL_BOOST + APPLICATION_BOOST)
    # 同 label 不同 application，只拿 label 的分
    assert out["same_label_other_app"]["score"] == pytest.approx(0.5 + LABEL_BOOST)


def test_cross_label_survives_and_ranks_lower(taxonomy: BipTaxonomy):
    """核心保证：跨 label 的知识不被过滤，只是排在后面。"""
    out = apply_taxonomy_boost(items(), taxonomy, module_boost=["流程中心"])
    ids = [r["id"] for r in out]
    assert "cross_label" in ids, "跨 label 知识被丢了——这是硬过滤行为，不允许"
    assert by_id(out)["cross_label"]["score"] == pytest.approx(0.5), "跨 label 不该被减分"
    assert ids.index("same_label") < ids.index("cross_label")


def test_project_boost_is_light(taxonomy: BipTaxonomy):
    out = by_id(apply_taxonomy_boost(items(), taxonomy, module_boost=[], project_key="LCZX"))
    assert out["same_label"]["score"] == pytest.approx(0.5 + PROJECT_BOOST)
    assert out["cross_label"]["score"] == pytest.approx(0.5)
    assert PROJECT_BOOST < LABEL_BOOST, "project 只是统计维度，权重必须低于 label"


def test_global_project_key_does_not_boost(taxonomy: BipTaxonomy):
    """_global 是"全局可见"，不是一个真实项目，不该参与项目加权。"""
    out = by_id(apply_taxonomy_boost(items(), taxonomy, project_key="_global"))
    assert out["same_label_other_app"]["score"] == pytest.approx(0.5)


def test_excluded_items_removed(taxonomy: BipTaxonomy):
    out = apply_taxonomy_boost(items(), taxonomy, module_boost=["流程中心"])
    assert "junk" not in [r["id"] for r in out]


def test_excluded_removed_even_without_any_boost(taxonomy: BipTaxonomy):
    """没有任何加权信号时也要剔除——早退路径不能漏掉它。"""
    out = apply_taxonomy_boost(items(), taxonomy)
    assert "junk" not in [r["id"] for r in out]
    assert len(out) == 3


# ---------------------------------------------------------------- 过渡期兼容


def test_legacy_directory_names_still_boost(taxonomy: BipTaxonomy):
    """索引还没回填时 l1/l2 是目录名，原有加分必须保留，否则过渡期效果塌陷。"""
    legacy_items = [
        {"id": "old", "l1_module": "bip-workflow", "l2_module": "", "score": 0.5},
        {"id": "other", "l1_module": "打印", "l2_module": "", "score": 0.5},
    ]
    out = by_id(apply_taxonomy_boost(legacy_items, taxonomy, module_boost=["bip-workflow"]))
    assert out["old"]["score"] == pytest.approx(0.5 + LEGACY_MODULE_BOOST)
    assert out["other"]["score"] == pytest.approx(0.5)


def test_unavailable_taxonomy_degrades_to_legacy(tmp_path: Path):
    """快照缺失时仍按目录名加权，等同改造前行为。"""
    tax = BipTaxonomy(path=tmp_path / "nope.json")
    legacy_items = [{"id": "old", "l1_module": "bip-workflow", "l2_module": "", "score": 0.5}]
    out = apply_taxonomy_boost(legacy_items, tax, module_boost=["bip-workflow"])
    assert out[0]["score"] == pytest.approx(0.5 + LEGACY_MODULE_BOOST)


# ---------------------------------------------------------------- 边界


def test_empty_inputs_never_raise(taxonomy: BipTaxonomy):
    assert apply_taxonomy_boost([], taxonomy) == []
    assert apply_taxonomy_boost(items(), taxonomy, module_boost=None) is not None
    assert apply_taxonomy_boost(items(), taxonomy, module_boost=[""]) is not None


def test_missing_score_key_defaults_to_zero(taxonomy: BipTaxonomy):
    rows = [{"id": "x", "l1_module": "数字化建模", "l2_module": ""}]
    out = apply_taxonomy_boost(rows, taxonomy, module_boost=["流程中心"])
    assert out[0]["score"] == pytest.approx(LABEL_BOOST)


def test_two_level_module_value_parsed(taxonomy: BipTaxonomy):
    """领域模块常见形态是 父|子，两段都要参与解析。"""
    out = by_id(apply_taxonomy_boost(
        items(), taxonomy, module_boost=["流程中心|工作流设计(含所有属性设置)"]
    ))
    assert out["same_label"]["score"] > 0.5


def test_boost_targets_cached(taxonomy: BipTaxonomy):
    first = taxonomy.resolve_boost_targets(["流程中心"])
    second = taxonomy.resolve_boost_targets(["流程中心"])
    assert first is second, "解析结果应命中缓存，避免每次检索重算"
