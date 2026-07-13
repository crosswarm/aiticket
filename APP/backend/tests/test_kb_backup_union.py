"""KB 受保护数据备份/恢复：高水位并集（只增不减）根治覆盖率流失。

回归根因（2026-07-13）：旧备份抓当下 live 值追加为快照+只留3份+restore取最新。
rebuild「dump→物理全清→内存回填」被打断会把 compiled 砸到低谷(如13)，备份把低谷当真相、
把好快照(132)挤出窗口 → restore 恢复低值 → 覆盖率棘轮式下滑、数据永久流失。
改为并集只增不减：备份永远保有历史最大集。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kb_runtime_service as krs


class _FakeHybridIndex:
    def __init__(self, rows=None):
        self._rows = [dict(r) for r in (rows or [])]

    def set_live(self, rows):
        self._rows = [dict(r) for r in rows]

    def list_by_source_kinds(self, source_kinds, top_k=500):
        return [dict(r) for r in self._rows if r.get("source_kind") in source_kinds]

    def count_by_source_kind(self, source_kind):
        return sum(1 for r in self._rows if r.get("source_kind") == source_kind)

    def add_item(self, item, text, source_mtime=None):
        row = dict(item)
        row["content"] = text
        row.setdefault("source_kind", "kb_compiled")
        # upsert 语义：同 content_id 覆盖
        self._rows = [r for r in self._rows if r.get("content_id") != row.get("content_id")]
        self._rows.append(row)
        return 1


def _svc(tmp_path, rows=None):
    s = krs.KnowledgeRuntimeService.__new__(krs.KnowledgeRuntimeService)
    s.project_root = tmp_path
    s._data_root = tmp_path / "APP" / "data"
    s._compiled_backup_path = s._data_root / "sqlite" / "kb_compiled_backup.json"
    s.hybrid_index = _FakeHybridIndex(rows)
    return s


def _rows(n, start=0):
    return [
        {"content_id": f"kb_compiled:t{i}", "source_kind": "kb_compiled",
         "name": f"话题{i}", "content": f"正文{i}"}
        for i in range(start, start + n)
    ]


def test_backup_union_does_not_shrink_when_live_low(tmp_path):
    """核心回归：先备份 132 条，live 砸到 13 条，再备份 → 备份仍是 132（并集只增不减）。"""
    s = _svc(tmp_path, _rows(132))
    n1 = s._backup_preserved_kinds()
    assert n1 == 132
    # 模拟 rebuild 被打断：live 只剩 13 条
    s.hybrid_index.set_live(_rows(13))
    n2 = s._backup_preserved_kinds()
    assert n2 == 132, f"低 live 摧毁了高备份：{n2}"
    # 磁盘上的备份并集应为 132
    data = json.loads(s._compiled_backup_path.read_text(encoding="utf-8"))
    assert len(krs.KnowledgeRuntimeService._load_backup_union(data)) == 132


def test_backup_union_grows_with_new_items(tmp_path):
    """新编译的话题并入 → 备份增长。"""
    s = _svc(tmp_path, _rows(100))
    assert s._backup_preserved_kinds() == 100
    s.hybrid_index.set_live(_rows(136))  # 补编译到 136
    assert s._backup_preserved_kinds() == 136


def test_restore_restores_full_union_not_latest(tmp_path):
    """restore 恢复整个并集，而非最新那份低快照。"""
    seed = _svc(tmp_path, _rows(132))
    seed._backup_preserved_kinds()
    # 新实例 index 空
    s = _svc(tmp_path, rows=[])
    assert s.hybrid_index.count_by_source_kind("kb_compiled") == 0
    restored = s._restore_from_backup()
    assert restored == 132
    assert s.hybrid_index.count_by_source_kind("kb_compiled") == 132


def test_restore_reads_old_snapshot_list_format(tmp_path):
    """向后兼容：旧 list-of-snapshots 格式（35/112/13）→ 恢复三者并集（不同 id 全恢复）。"""
    s = _svc(tmp_path, rows=[])
    s._compiled_backup_path.parent.mkdir(parents=True, exist_ok=True)
    old = [
        {"ts": 1, "items": _rows(35, start=0)},
        {"ts": 2, "items": _rows(112, start=0)},   # 覆盖 0..111
        {"ts": 3, "items": _rows(13, start=200)},  # 另一批 200..212
    ]
    s._compiled_backup_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    restored = s._restore_from_backup()
    # 并集 = 0..111 (112) ∪ 200..212 (13) = 125
    assert restored == 125


def test_sync_low_live_then_restore_recovers_full(tmp_path):
    """端到端：备份满集→live被砸低→_backup仍满→restore拉回满。"""
    s = _svc(tmp_path, _rows(130))
    pc = s._backup_preserved_kinds()          # 130
    s.hybrid_index.set_live(_rows(13))        # rebuild 打断留 13
    pc2 = s._backup_preserved_kinds()         # 并集仍 130
    assert pc2 == 130
    s.hybrid_index.set_live([])               # 极端：全空
    restored = s._restore_from_backup()
    assert restored == 130
    assert s.hybrid_index.count_by_source_kind("kb_compiled") == 130
