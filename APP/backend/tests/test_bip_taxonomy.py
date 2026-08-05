"""BIP 产品分类解析的单元测试。

覆盖三级证据（service_code > 目录名 > 别名）、同名消歧、以及最关键的
「快照缺失必须优雅降级」——172 是气隙机，任何加载失败都不能把索引构建拖崩。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bip_taxonomy import BipTaxonomy, Classification, safe_dirname  # noqa: E402


@pytest.fixture(scope="module")
def taxonomy() -> BipTaxonomy:
    return BipTaxonomy()


# ---------------------------------------------------------------- 快照自身


def test_snapshot_loads(taxonomy: BipTaxonomy):
    assert taxonomy.available is True
    assert len(taxonomy.labels) > 100
    assert len(taxonomy.applications) > 1000


def test_missing_snapshot_degrades_gracefully(tmp_path: Path):
    """快照不存在时不抛异常，classify 原样退回目录名。"""
    tax = BipTaxonomy(path=tmp_path / "nope.json")
    assert tax.available is False
    result = tax.classify(top_category="业务流", second_category="设计")
    assert result.label_name == "业务流"       # 退回原值
    assert result.application_name == "设计"
    assert result.evidence == "unavailable"


def test_corrupt_snapshot_degrades_gracefully(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    tax = BipTaxonomy(path=bad)
    assert tax.available is False
    assert tax.classify(top_category="X").label_name == "X"


# ---------------------------------------------------------------- 证据 1：service_code


def test_service_code_in_text_wins(taxonomy: BipTaxonomy):
    """正文里的 service_code 是最强证据，即使目录名指向别处也以它为准。"""
    text = "工作流设计（tab, id=XTLCZX007-nav）默认加载「业务模型管理」子菜单"
    result = taxonomy.classify(top_category="随便什么目录", text=text)
    assert result.label_code == "PF"
    assert result.label_name == "数字化建模"
    assert result.application_name == "工作流"
    assert result.evidence == "service_code"
    assert result.service_codes == ["XTLCZX007"]


def test_service_code_majority_vote(taxonomy: BipTaxonomy):
    """多个 service_code 时按 application 取众数，避免被一次性提及带偏。"""
    text = "GZTTMP012 GZTTMP011 GZTTMP040 参考 XTLCZX007"
    result = taxonomy.classify(top_category="UI模板", text=text)
    assert result.application_name == "UI模板"
    assert result.label_code == "BMM"


def test_unknown_service_code_ignored(taxonomy: BipTaxonomy):
    """长得像编码但不在主数据里的串不能污染归属。"""
    text = "参见 ABCDEF123 与 PFUVH3B4LVJDZFYLH4DD"
    result = taxonomy.classify(top_category="UI模板", text=text)
    assert result.evidence != "service_code"
    assert result.application_name == "UI模板"


def test_directory_beats_service_code(taxonomy: BipTaxonomy):
    """实测回归：977 篇实跑中这篇曾被正文提到的 GZTACT015 带到「权限管理」。

    正文提及 service_code 往往是引用（"权限在用户管理里配"），
    而目录是人工批量组织的，归属上更可信。
    """
    text = "UI模板管理-模板列表上复制按钮功能说明。权限相关见 GZTACT015 用户管理。"
    result = taxonomy.classify(top_category="UI模板", text=text)
    assert result.application_name == "UI模板"
    assert result.label_code == "BMM"
    assert result.evidence == "directory"


# ---------------------------------------------------------------- 证据 4：二级目录


def test_second_category_resolves_label(taxonomy: BipTaxonomy):
    """云平台是聚合目录，二级目录名才是真正的 label。"""
    result = taxonomy.classify(top_category="云平台", second_category="开发平台")
    assert result.label_name == "开发平台"
    assert result.domain_cloud_name == "云平台"
    assert result.evidence == "second_category"


def test_second_category_resolves_application(taxonomy: BipTaxonomy):
    """二级目录名也可能直接是 application 名（可跨领域云）。"""
    result = taxonomy.classify(top_category="云平台", second_category="三方集成资产")
    assert result.application_name == "三方集成资产"
    assert result.evidence == "second_category"


def test_label_named_dir_does_not_swallow_second_level(taxonomy: BipTaxonomy):
    """E2E 回归：「业务模型管理」既是 label(BMM) 又是 application(BMMMM)。

    知识按 KB/<label>/<application>/ 存放，一级目录若被当成 application 解释，
    l2 会被填成「业务模型管理」，二级目录「UI模板」就被吃掉了。
    """
    result = taxonomy.classify(top_category="业务模型管理", second_category="UI模板")
    assert result.label_code == "BMM"
    assert result.label_name == "业务模型管理"
    assert result.application_name == "UI模板", "二级目录被一级的同名 application 吃掉了"


def test_upload_layout_roundtrip(taxonomy: BipTaxonomy):
    """上传落盘用的 <label>/<application> 结构必须能原样解析回来。"""
    result = taxonomy.classify(top_category="数字化建模", second_category="工作流")
    assert result.label_name == "数字化建模"
    assert result.application_name == "工作流"


def test_directory_still_matches_application_only_names(taxonomy: BipTaxonomy):
    """历史目录名多是 application 名且不是 label 名，优先 label 不能误伤它们。"""
    for name, expect_label in [("UI模板", "业务模型管理"), ("业务流", "数字化建模")]:
        result = taxonomy.classify(top_category=name)
        assert result.application_name == name
        assert result.label_name == expect_label


def test_aggregate_dir_without_match_falls_back(taxonomy: BipTaxonomy):
    """聚合目录下二级名对不上主数据时，退回而不是硬塞。"""
    result = taxonomy.classify(top_category="云平台", second_category="监控中心")
    assert result.evidence == "fallback"


def test_strict_mode_blocks_cross_domain_cloud(taxonomy: BipTaxonomy):
    """实测回归：编译主题「主数据」曾被匹配到财务云的「境外财资/主数据」。

    topic 名是工单聚类出来的自由文本，不像目录名那样受控，
    宁可不归属也不能归到别的领域云去。
    """
    loose = taxonomy.classify(top_category="主数据")
    assert loose.domain_cloud_name == "财务云"      # 不设限就会串过去

    strict = taxonomy.classify(top_category="主数据", strict=True)
    assert strict.evidence == "fallback"
    assert strict.label_code == ""


def test_strict_mode_keeps_preferred_domain_clouds(taxonomy: BipTaxonomy):
    """限定不能误伤应用平台/云平台自己的条目。"""
    result = taxonomy.classify(top_category="专业开发", strict=True)
    assert result.domain_cloud_name == "云平台"
    result = taxonomy.classify(top_category="UI模板", strict=True)
    assert result.label_code == "BMM"


def test_excluded_source_files(taxonomy: BipTaxonomy):
    """AITicket 自身的源码目录被 KB 扫描器误收，不是业务知识。"""
    result = taxonomy.classify(top_category="APP", second_category="backend")
    assert result.is_excluded is True
    assert result.in_bip is False
    assert result.is_external is False


# ---------------------------------------------------------------- 证据 2：目录名


@pytest.mark.parametrize(
    "top_category,expect_label,expect_app",
    [
        ("UI模板", "业务模型管理", "UI模板"),
        ("业务流", "数字化建模", "业务流"),
    ],
)
def test_directory_exact_match(taxonomy: BipTaxonomy, top_category, expect_label, expect_app):
    result = taxonomy.classify(top_category=top_category)
    assert result.label_name == expect_label
    assert result.application_name == expect_app
    assert result.evidence == "directory"


# ---------------------------------------------------------------- 证据 3：别名


@pytest.mark.parametrize(
    "top_category,expect_label,expect_app",
    [
        ("bip-workflow", "数字化建模", "工作流"),
        ("流程中心", "数字化建模", "工作流"),
        ("打印", "业务模型管理", "打印模板"),
        ("导入导出", "业务模型管理", "导入导出模板"),
        ("规则", "业务模型管理", "规则引擎"),
        ("消息", "业务模型管理", "消息平台"),
        ("组织", "数字化建模", "组织管理"),
        ("权限", "数字化建模", "权限管理"),
        ("元数据", "数字化建模", "元数据服务"),
        ("配置迁移", "工具集", "迁移工具"),
    ],
)
def test_alias_match(taxonomy: BipTaxonomy, top_category, expect_label, expect_app):
    result = taxonomy.classify(top_category=top_category)
    assert result.label_name == expect_label
    assert result.application_name == expect_app
    assert result.evidence == "alias"


def test_alias_label_only(taxonomy: BipTaxonomy):
    """YPD 开发框架没有独立 application，只落到 label。"""
    result = taxonomy.classify(top_category="YPD开发框架")
    assert result.label_code == "PF"
    assert result.label_name == "数字化建模"
    assert result.application_name == ""


def test_external_not_mapped(taxonomy: BipTaxonomy):
    """金蝶是竞品，BIP 体系里没有对应，必须标 _external 而不是硬塞一个 label。"""
    result = taxonomy.classify(top_category="kingdee-workflow")
    assert result.label_code == "_external"
    assert result.is_external is True


# ---------------------------------------------------------------- 同名消歧


def test_ambiguous_name_prefers_target_domain_cloud(taxonomy: BipTaxonomy):
    """「组织管理」在应用平台和人力云各有一个，KB 场景应落应用平台。"""
    result = taxonomy.classify(top_category="组织")
    assert result.label_code == "PF"
    assert result.domain_cloud_name == "应用平台"


def test_ambiguous_prefers_plain_code(taxonomy: BipTaxonomy):
    """「权限管理」有 GZTACT / ALONE_GZTACT / auth，应取主编码 GZTACT。"""
    result = taxonomy.classify(top_category="权限")
    assert result.application_code == "GZTACT"


def test_tenant_built_apps_excluded(taxonomy: BipTaxonomy):
    """同名候选里既有租户自建又有正式应用时，必须选正式的那个。

    主数据的 application 层混着大量租户自建应用（AT1/GT 前缀），
    它们会和正式应用重名，选错了整篇文档就归到别的 label 去了。
    """
    checked = 0
    for name, codes in taxonomy._applications_by_name.items():
        if len(codes) < 2:
            continue
        flags = [bool(taxonomy.applications[c].get("tenant_built")) for c in codes]
        if not (any(flags) and not all(flags)):
            continue  # 只看"混合"场景
        picked = taxonomy._pick_application(name)
        assert not taxonomy.applications[picked].get("tenant_built"), f"{name} 选中了租户自建应用 {picked}"
        checked += 1
    assert checked > 0, "快照里没有混合同名场景，用例失去意义"


# ---------------------------------------------------------------- 未知输入


def test_unknown_directory_falls_back(taxonomy: BipTaxonomy):
    result = taxonomy.classify(top_category="不存在的目录xyz", second_category="子目录")
    assert result.label_name == "不存在的目录xyz"
    assert result.application_name == "子目录"
    assert result.evidence == "fallback"


def test_empty_input_never_raises(taxonomy: BipTaxonomy):
    assert isinstance(taxonomy.classify(), Classification)
    assert isinstance(taxonomy.classify(top_category="", text=""), Classification)
    assert isinstance(taxonomy.classify(text=None), Classification)


# ---------------------------------------------------------------- 覆盖表


def test_override_wins_over_everything(taxonomy: BipTaxonomy, tmp_path: Path):
    override = tmp_path / "ov.json"
    override.write_text(
        json.dumps({"aliases": {"随便什么目录": {"application": "打印模板"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    tax = BipTaxonomy(overrides_path=override)
    # 即使正文里有 service_code，人工覆盖也应优先
    result = tax.classify(top_category="随便什么目录", text="XTLCZX007")
    assert result.application_name == "打印模板"
    assert result.evidence == "override"


# ---------------------------------------------------------------- 反查


def test_resolve_service_chain(taxonomy: BipTaxonomy):
    chain = taxonomy.resolve_service("XTLCZX0010")
    assert chain["service_name"] == "审批矩阵"
    assert chain["application_name"] == "工作流"
    assert chain["label_name"] == "数字化建模"
    assert chain["domain_cloud_name"] == "应用平台"


def test_resolve_service_unknown(taxonomy: BipTaxonomy):
    assert taxonomy.resolve_service("NOPE999") is None


# ---------------------------------------------------------------- 目录名净化


def test_safe_dirname_strips_path_separators():
    assert safe_dirname("批次/序列号") == "批次_序列号"
    assert safe_dirname("a\\b:c*d?e") == "a_b_c_d_e"
    assert safe_dirname("  正常名称  ") == "正常名称"
    assert safe_dirname("") == ""
    assert safe_dirname("...") == ""


def test_sanitized_application_name_still_resolves(taxonomy: BipTaxonomy):
    """落盘时「批次/序列号」变成「批次_序列号」，解析必须能找回同一个应用。

    否则上传的知识落到目录里就再也归不了属——两边用的是同一个 safe_dirname。
    """
    original = taxonomy.classify(top_category="批次/序列号")
    sanitized = taxonomy.classify(top_category="批次_序列号")
    assert original.application_code == "DPMBTSN"
    assert sanitized.application_code == original.application_code
    assert sanitized.label_code == original.label_code
