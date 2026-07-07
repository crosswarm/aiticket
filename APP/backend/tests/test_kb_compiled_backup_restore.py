"""KB 编译条目 备份/恢复 路径一致性 + 启动自愈 + sync 强校验。

回归根因 R0：_backup 写 _data_root/sqlite/，_restore 却读 project_root/data/sqlite/
（缺 APP/ 段）→ restore 永远找不到备份、每次 sync 后 kb_compiled 归零，
挂在编译文章上的知识图谱关系随之消失。收敛到 self._compiled_backup_path 单一真相源。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kb_runtime_service as krs


class _FakeHybridIndex:
    """最小 hybrid_index 替身：内存存储受保护条目，避开 chroma/bge。"""

    def __init__(self, rows=None):
        self._rows = [dict(r) for r in (rows or [])]

    def list_by_source_kinds(self, source_kinds, top_k=500):
        return [dict(r) for r in self._rows if r.get("source_kind") in source_kinds]

    def count_by_source_kind(self, source_kind):
        return sum(1 for r in self._rows if r.get("source_kind") == source_kind)

    def add_item(self, item, text, source_mtime=None):
        row = dict(item)
        row["content"] = text
        row.setdefault("source_kind", "kb_compiled")
        self._rows.append(row)
        return 1


def _make_service(tmp_path, rows=None):
    """绕过重量级 __init__，只装配备份/恢复需要的最小属性。"""
    svc = krs.KnowledgeRuntimeService.__new__(krs.KnowledgeRuntimeService)
    svc.project_root = tmp_path
    svc._data_root = tmp_path / "APP" / "data"
    svc._compiled_backup_path = svc._data_root / "sqlite" / "kb_compiled_backup.json"
    svc.hybrid_index = _FakeHybridIndex(rows)
    return svc


def _compiled_row(i):
    return {
        "content_id": f"kb_compiled:t{i}",
        "source_kind": "kb_compiled",
        "name": f"综合解析：话题{i}",
        "content": f"话题{i}正文",
    }


def test_backup_and_restore_use_identical_path(tmp_path):
    """核心回归：备份写路径 == 恢复读路径（历史 bug 是两者不一致）。"""
    svc = _make_service(tmp_path)
    # 单一真相源在 _data_root（含 APP/ 段），不是 project_root/data
    assert "APP" in svc._compiled_backup_path.parts
    assert svc._compiled_backup_path != svc.project_root / "data" / "sqlite" / "kb_compiled_backup.json"
    assert svc._compiled_backup_path == svc._data_root / "sqlite" / "kb_compiled_backup.json"


def test_backup_restore_roundtrip(tmp_path):
    """backup → 新实例 restore：条目数一致，走同一文件。"""
    rows = [_compiled_row(1), _compiled_row(2)]
    src = _make_service(tmp_path, rows)
    n = src._backup_preserved_kinds()
    assert n == 2
    assert src._compiled_backup_path.exists()

    # 新实例（index 空）从同一 tmp_path → 同一备份路径恢复
    dst = _make_service(tmp_path, rows=[])
    assert dst.hybrid_index.count_by_source_kind("kb_compiled") == 0
    restored = dst._restore_from_backup()
    assert restored == 2
    assert dst.hybrid_index.count_by_source_kind("kb_compiled") == 2


def test_auto_restore_when_empty(tmp_path):
    """sync 后 compiled=0 且备份在 → 启动自愈补回。"""
    src = _make_service(tmp_path, [_compiled_row(1)])
    src._backup_preserved_kinds()

    svc = _make_service(tmp_path, rows=[])
    assert svc.hybrid_index.count_by_source_kind("kb_compiled") == 0
    restored = svc.auto_restore_compiled_if_empty()
    assert restored == 1
    assert svc.hybrid_index.count_by_source_kind("kb_compiled") == 1


def test_auto_restore_noop_when_present(tmp_path):
    """非空不动：已有条目时启动自愈不重复恢复。"""
    src = _make_service(tmp_path, [_compiled_row(1)])
    src._backup_preserved_kinds()

    svc = _make_service(tmp_path, rows=[_compiled_row(1)])
    assert svc.auto_restore_compiled_if_empty() == 0
    assert svc.hybrid_index.count_by_source_kind("kb_compiled") == 1


def test_auto_restore_noop_when_no_backup(tmp_path):
    """无备份文件 → 自愈静默返回 0，不抛。"""
    svc = _make_service(tmp_path, rows=[])
    assert not svc._compiled_backup_path.exists()
    assert svc.auto_restore_compiled_if_empty() == 0


def test_assert_preserved_restored_raises_when_short(tmp_path):
    """恢复后仍不足 → raise（不再静默吞，让 schedule 标 failed）。"""
    svc = _make_service(tmp_path, rows=[])  # index 空 → count 0
    with pytest.raises(RuntimeError):
        svc._assert_preserved_restored(preserved_count=116, restored=0)


def test_assert_preserved_restored_ok_when_met(tmp_path):
    """恢复到位 → 不抛。"""
    svc = _make_service(tmp_path, rows=[_compiled_row(1)])
    svc._assert_preserved_restored(preserved_count=1, restored=1)
