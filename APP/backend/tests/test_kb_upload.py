"""KB 上传落盘的单元测试。

重点是**不能越界写文件**和**不能静默丢知识**：
文件名来自用户输入，目录名来自主数据（里面确实有带斜杠的名字）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bip_taxonomy import BipTaxonomy  # noqa: E402
from kb_upload import (  # noqa: E402
    MAX_BYTES,
    MAX_FILES,
    UploadRejected,
    build_taxonomy_tree,
    resolve_target_dir,
    safe_filename,
    save_uploads,
)


@pytest.fixture(scope="module")
def taxonomy() -> BipTaxonomy:
    return BipTaxonomy()


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "KB"
    root.mkdir()
    return root


# ---------------------------------------------------------------- 文件名安全


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("正常文档.md", "正常文档.md"),
        ("../../../etc/passwd", "passwd"),
        ("a/b/c.md", "c.md"),
        ("....md", "md"),          # 前导点全剥掉，避免造出隐藏文件
        ("", "untitled"),
        ("   ", "untitled"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32",   # 反斜杠在 POSIX 上不是分隔符，靠字符替换兜住
        "a/b/../../../c.md",
        "/absolute/path.md",
        ".hidden",
        "a\x00b.md",
    ],
)
def test_safe_filename_is_always_a_plain_name(raw):
    """断言安全属性而不是具体字符串——分隔符语义随平台不同。"""
    name = safe_filename(raw)
    assert name, "净化后不能为空"
    assert "/" not in name and "\\" not in name
    assert not name.startswith(".")
    assert Path(name).name == name, "结果必须是单层文件名"


def test_safe_filename_strips_control_chars():
    assert "\n" not in safe_filename("a\nb.md")
    assert safe_filename("a\x00b.md") == "a_b.md"


def test_path_traversal_cannot_escape(kb_root: Path, taxonomy: BipTaxonomy):
    """恶意文件名不能把文件写到 KB 之外。"""
    result = save_uploads(kb_root, taxonomy, "PF", "", [("../../pwned.md", b"x")])
    written = Path(kb_root.parent / result["saved"][0]["rel_path"]).resolve()
    assert kb_root.resolve() in written.parents


# ---------------------------------------------------------------- 归属校验


def test_resolve_target_dir(kb_root: Path, taxonomy: BipTaxonomy):
    target, label, app = resolve_target_dir(kb_root, taxonomy, "PF", "GZTFLOW")
    assert target == kb_root / "数字化建模" / "工作流"
    assert label["name"] == "数字化建模"
    assert app["name"] == "工作流"


def test_resolve_target_dir_label_only(kb_root: Path, taxonomy: BipTaxonomy):
    target, _, app = resolve_target_dir(kb_root, taxonomy, "PF")
    assert target == kb_root / "数字化建模"
    assert app is None


def test_slash_in_application_name_does_not_split_path(kb_root: Path, taxonomy: BipTaxonomy):
    """application「批次/序列号」直接拼路径会凭空多切一级目录。"""
    target, _, _ = resolve_target_dir(kb_root, taxonomy, "MD", "DPMBTSN")
    assert target.name == "批次_序列号"
    assert target.parent == kb_root / "基础数据"


def test_unknown_label_rejected(kb_root: Path, taxonomy: BipTaxonomy):
    with pytest.raises(UploadRejected, match="label_code"):
        resolve_target_dir(kb_root, taxonomy, "NOPE")


def test_mismatched_application_rejected(kb_root: Path, taxonomy: BipTaxonomy):
    """application 必须真的属于所选 label，否则归属就是错的。"""
    with pytest.raises(UploadRejected, match="不属于"):
        resolve_target_dir(kb_root, taxonomy, "BMM", "GZTFLOW")  # 工作流属于 PF


def test_unavailable_taxonomy_rejected(kb_root: Path, tmp_path: Path):
    tax = BipTaxonomy(path=tmp_path / "nope.json")
    with pytest.raises(UploadRejected, match="快照不可用"):
        save_uploads(kb_root, tax, "PF", "", [("a.md", b"x")])


# ---------------------------------------------------------------- 落盘


def test_saves_to_taxonomy_path(kb_root: Path, taxonomy: BipTaxonomy):
    result = save_uploads(kb_root, taxonomy, "PF", "GZTFLOW", [("指南.md", b"hello")])
    assert result["status"] == "success"
    written = kb_root / "数字化建模" / "工作流" / "指南.md"
    assert written.read_bytes() == b"hello"
    assert result["saved"][0]["parsable"] is True


def test_duplicate_name_does_not_overwrite(kb_root: Path, taxonomy: BipTaxonomy):
    """同名覆盖会静默丢知识——KB 里同名文档很常见。"""
    save_uploads(kb_root, taxonomy, "PF", "", [("a.md", b"first")])
    save_uploads(kb_root, taxonomy, "PF", "", [("a.md", b"second")])
    folder = kb_root / "数字化建模"
    assert (folder / "a.md").read_bytes() == b"first"
    assert (folder / "a(1).md").read_bytes() == b"second"


def test_unsupported_extension_skipped(kb_root: Path, taxonomy: BipTaxonomy):
    result = save_uploads(kb_root, taxonomy, "PF", "", [("evil.exe", b"x"), ("ok.md", b"y")])
    assert [s["filename"] for s in result["skipped"]] == ["evil.exe"]
    assert [s["filename"] for s in result["saved"]] == ["ok.md"]


def test_pdf_allowed_but_flagged_unparsable(kb_root: Path, taxonomy: BipTaxonomy):
    """pdf 能上传，但 KB 提不出正文，要让用户知道。"""
    result = save_uploads(kb_root, taxonomy, "PF", "", [("发版说明.pdf", b"%PDF-")])
    assert result["saved"][0]["parsable"] is False


def test_oversize_skipped(kb_root: Path, taxonomy: BipTaxonomy):
    result = save_uploads(kb_root, taxonomy, "PF", "", [("big.md", b"x" * (MAX_BYTES + 1))])
    assert result["saved"] == []
    assert "超过" in result["skipped"][0]["reason"]


def test_empty_file_skipped(kb_root: Path, taxonomy: BipTaxonomy):
    result = save_uploads(kb_root, taxonomy, "PF", "", [("empty.md", b"")])
    assert result["skipped"][0]["reason"] == "空文件"


def test_too_many_files_rejected(kb_root: Path, taxonomy: BipTaxonomy):
    files = [(f"{i}.md", b"x") for i in range(MAX_FILES + 1)]
    with pytest.raises(UploadRejected, match="最多"):
        save_uploads(kb_root, taxonomy, "PF", "", files)


def test_no_files_rejected(kb_root: Path, taxonomy: BipTaxonomy):
    with pytest.raises(UploadRejected):
        save_uploads(kb_root, taxonomy, "PF", "", [])


def test_all_skipped_reports_error_status(kb_root: Path, taxonomy: BipTaxonomy):
    result = save_uploads(kb_root, taxonomy, "PF", "", [("a.exe", b"x")])
    assert result["status"] == "error"


# ---------------------------------------------------------------- 归属树


def test_taxonomy_tree_excludes_tenant_built(taxonomy: BipTaxonomy):
    tree = build_taxonomy_tree(taxonomy)
    for label in tree["labels"]:
        for app in label["applications"]:
            assert not app["code"].startswith(("AT1", "GT")), f"租户自建应用不该出现：{app}"


def test_taxonomy_tree_excludes_scratch_named_apps(taxonomy: BipTaxonomy):
    """UI 实测回归：数字化建模下拉框最前面是「0租户应用」「0816专业版-勿动」。

    这批 code 看不出是草稿件（不是 AT1/GT 前缀），只能从名字判断。
    """
    tree = build_taxonomy_tree(taxonomy, domain_cloud="PFC")
    names = [app["name"] for label in tree["labels"] for app in label["applications"]]
    assert names, "过滤不能把所有应用都滤掉"
    for bad in ("勿动", "勿删", "0租户", "测试"):
        assert not any(bad in n for n in names), f"下拉框里仍有草稿件：{[n for n in names if bad in n]}"


def test_scratch_filter_keeps_real_applications(taxonomy: BipTaxonomy):
    """过滤不能误伤真实应用。"""
    tree = build_taxonomy_tree(taxonomy, domain_cloud="PFC")
    pf = next(label for label in tree["labels"] if label["code"] == "PF")
    names = {app["name"] for app in pf["applications"]}
    assert {"工作流", "业务流", "权限管理", "组织管理"} <= names


def test_taxonomy_tree_filter_by_domain_cloud(taxonomy: BipTaxonomy):
    tree = build_taxonomy_tree(taxonomy, domain_cloud="PFC")
    assert tree["labels"]
    assert all(label["domain_cloud"] == "PFC" for label in tree["labels"])
    names = {label["name"] for label in tree["labels"]}
    assert {"数字化建模", "业务模型管理", "工具集"} <= names


def test_taxonomy_tree_applications_belong_to_label(taxonomy: BipTaxonomy):
    tree = build_taxonomy_tree(taxonomy, domain_cloud="PFC")
    pf = next(label for label in tree["labels"] if label["code"] == "PF")
    assert "工作流" in {app["name"] for app in pf["applications"]}
