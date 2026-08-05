"""索引孤儿清理的单元测试。

最重要的一条：**只能清理磁盘上真有文件的来源**。
kb_compiled 是主题编译产物、ticket_case 来自 Jira，它们的 source_rel_path
不指向任何文件——一旦纳入"文件不存在就删"的判断，会被整批误删。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_runtime_service import KnowledgeRuntimeService  # noqa: E402


class FakeIndex:
    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.deleted: list[str] = []

    def list_document_paths(self, source_kinds: tuple[str, ...] = ()):
        if not source_kinds:
            return list(self.docs)
        return [d for d in self.docs if d.get("source_kind") in source_kinds]

    def delete_item(self, content_id: str) -> bool:
        self.deleted.append(content_id)
        return True


def make_service(tmp_path: Path, docs: list[dict]) -> KnowledgeRuntimeService:
    """绕过 __init__——真实构造会连 chroma、加载嵌入模型，单测不需要。"""
    svc = KnowledgeRuntimeService.__new__(KnowledgeRuntimeService)
    svc.project_root = tmp_path
    svc.kb_root = tmp_path / "KB"
    svc.kb_root.mkdir(exist_ok=True)
    svc.hybrid_index = FakeIndex(docs)
    return svc


@pytest.fixture
def svc(tmp_path: Path):
    live = tmp_path / "KB" / "数字化建模" / "工作流"
    live.mkdir(parents=True)
    (live / "在的.md").write_bytes(b"x")
    return make_service(tmp_path, [
        {"content_id": "CNT-0001", "source_kind": "kb_local",
         "source_rel_path": "KB/数字化建模/工作流/在的.md", "name": "在的", "l1_module": "数字化建模"},
        {"content_id": "CNT-0002", "source_kind": "kb_local",
         "source_rel_path": "KB/APP/backend/README.md", "name": "孤儿", "l1_module": "_excluded"},
        {"content_id": "CNT-0003", "source_kind": "kb_compiled",
         "source_rel_path": "compiled/工作流", "name": "综合解析：工作流", "l1_module": ""},
    ])


# ---------------------------------------------------------------- 安全边界


def test_compiled_source_kind_is_rejected(svc):
    """kb_compiled 没有磁盘文件，纳入清理会被整批误删——必须直接拒绝。"""
    with pytest.raises(ValueError, match="没有对应文件"):
        svc.prune_missing(dry_run=False, source_kinds=("kb_compiled",))
    assert svc.hybrid_index.deleted == []


def test_ticket_case_source_kind_is_rejected(svc):
    with pytest.raises(ValueError):
        svc.prune_missing(source_kinds=("kb_local", "ticket_case"))


def test_default_scope_never_touches_compiled(svc):
    """默认范围必须把 kb_compiled 排除在外。"""
    result = svc.prune_missing(dry_run=False)
    assert "kb_compiled" not in result["scanned_source_kinds"]
    assert "CNT-0003" not in svc.hybrid_index.deleted


# ---------------------------------------------------------------- 判定


def test_dry_run_is_default_and_deletes_nothing(svc):
    result = svc.prune_missing()
    assert result["dry_run"] is True
    assert result["orphan_count"] == 1
    assert result["deleted"] == 0
    assert svc.hybrid_index.deleted == []


def test_apply_deletes_only_orphans(svc):
    result = svc.prune_missing(dry_run=False)
    assert result["deleted"] == 1
    assert svc.hybrid_index.deleted == ["CNT-0002"]
    assert "CNT-0001" not in svc.hybrid_index.deleted, "存在的文件不能被删"


def test_orphan_report_carries_context(svc):
    orphan = svc.prune_missing()["orphans"][0]
    assert orphan["content_id"] == "CNT-0002"
    assert orphan["source_rel_path"] == "KB/APP/backend/README.md"
    assert orphan["name"] == "孤儿"


# ---------------------------------------------------------------- 路径形态


def test_path_without_kb_prefix_is_recognized(tmp_path: Path):
    """索引里两种路径形态都有，认错会把正常文档判成孤儿。"""
    live = tmp_path / "KB" / "打印"
    live.mkdir(parents=True)
    (live / "a.md").write_bytes(b"x")
    svc = make_service(tmp_path, [
        {"content_id": "C1", "source_kind": "kb_local", "source_rel_path": "打印/a.md", "name": "", "l1_module": ""},
    ])
    assert svc.prune_missing()["orphan_count"] == 0


def test_empty_path_is_not_treated_as_orphan(tmp_path: Path):
    """路径为空无法判断存在性，宁可留着也不误删。"""
    svc = make_service(tmp_path, [
        {"content_id": "C1", "source_kind": "kb_local", "source_rel_path": "", "name": "", "l1_module": ""},
        {"content_id": "C2", "source_kind": "kb_local", "source_rel_path": None, "name": "", "l1_module": ""},
    ])
    result = svc.prune_missing(dry_run=False)
    assert result["orphan_count"] == 0
    assert svc.hybrid_index.deleted == []


def test_nothing_to_prune_is_fine(tmp_path: Path):
    svc = make_service(tmp_path, [])
    result = svc.prune_missing(dry_run=False)
    assert result["orphan_count"] == 0
    assert result["deleted"] == 0
