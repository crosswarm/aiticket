"""kb_rechunk_trim 单测：用小型合成 sqlite 验证过切文档被正确截尾、FTS 同步、幂等。

不碰真实库、不加载 bge、不连 chroma(chroma 删除以回调注入)。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kb_rechunk_trim as krt


def _make_db(path, docs):
    """docs: {content_id: n_chunks}。造 documents + chunks + chunks_fts。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (content_id TEXT PRIMARY KEY, source_kind TEXT, name TEXT);
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY, content_id TEXT, chunk_index INTEGER,
            chunk_text TEXT, chunk_preview TEXT
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, chunk_text);
        """
    )
    for cid, n in docs.items():
        conn.execute("INSERT INTO documents VALUES (?,?,?)", (cid, "kb_local", cid))
        for i in range(1, n + 1):
            chunk_id = f"{cid}::chunk-{i:03d}"
            txt = f"{cid} body chunk {i} " + "内容" * 20
            conn.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?)",
                (chunk_id, cid, i, txt, txt[:240]),
            )
            conn.execute("INSERT INTO chunks_fts VALUES (?,?)", (chunk_id, txt))
    conn.commit()
    return conn


def _counts(conn, cid):
    n_chunks = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE content_id=?", (cid,)
    ).fetchone()[0]
    n_fts = conn.execute(
        "SELECT COUNT(*) FROM chunks_fts f JOIN chunks c ON f.chunk_id=c.chunk_id WHERE c.content_id=?"
        , (cid,)
    ).fetchone()[0]
    # fts 独立计数(即便对应 chunk 被删)：直接按 chunk_id 前缀
    n_fts_all = conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunk_id LIKE ?", (cid + "::%",)
    ).fetchone()[0]
    return n_chunks, n_fts_all


def test_plan_trim_identifies_oversized(tmp_path):
    conn = _make_db(tmp_path / "kb.db", {"big": 10, "small": 2, "mid": 5})
    plan = krt.plan_trim(conn, cap=3)
    assert plan["total_docs"] == 3
    assert plan["total_chunks"] == 17
    assert plan["affected_docs"] == 2  # big(10) + mid(5)
    assert plan["to_delete"] == (10 - 3) + (5 - 3)  # 9
    assert plan["post_chunks"] == 17 - 9  # 8
    over_ids = {cid for cid, _ in plan["over"]}
    assert over_ids == {"big", "mid"}
    conn.close()


def test_trim_doc_truncates_and_syncs_fts(tmp_path):
    conn = _make_db(tmp_path / "kb.db", {"big": 10, "small": 2})
    deleted_ids = []
    d = krt.trim_doc(conn, "big", cap=3, chroma_delete=deleted_ids.extend, id_batch=4)
    assert d == 7  # 删掉 index 4..10
    n_chunks, n_fts = _counts(conn, "big")
    assert n_chunks == 3  # 只剩前 3
    assert n_fts == 3     # FTS 同步删除
    # 保留的是前三个(chunk_index 1..3)
    kept = [r[0] for r in conn.execute(
        "SELECT chunk_index FROM chunks WHERE content_id='big' ORDER BY chunk_index")]
    assert kept == [1, 2, 3]
    # chroma 删除回调收到了正确的 7 个 id
    assert len(deleted_ids) == 7
    assert "big::chunk-004" in deleted_ids and "big::chunk-010" in deleted_ids
    assert "big::chunk-003" not in deleted_ids
    # 未过切文档不受影响
    assert _counts(conn, "small") == (2, 2)
    conn.close()


def test_trim_doc_idempotent(tmp_path):
    conn = _make_db(tmp_path / "kb.db", {"big": 10})
    first = krt.trim_doc(conn, "big", cap=3)
    second = krt.trim_doc(conn, "big", cap=3)  # 再跑一次
    assert first == 7
    assert second == 0  # 幂等：第二次无删除
    assert _counts(conn, "big") == (3, 3)
    conn.close()


def test_trim_doc_under_cap_noop(tmp_path):
    conn = _make_db(tmp_path / "kb.db", {"small": 2})
    d = krt.trim_doc(conn, "small", cap=3)
    assert d == 0
    assert _counts(conn, "small") == (2, 2)
    conn.close()


def test_full_trim_reduces_total(tmp_path):
    conn = _make_db(tmp_path / "kb.db", {"a": 8, "b": 8, "c": 1})
    plan = krt.plan_trim(conn, cap=3)
    for cid, _ in plan["over"]:
        krt.trim_doc(conn, cid, cap=3)
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert total == 3 + 3 + 1  # a→3, b→3, c→1
    # 复跑 plan：已达标
    plan2 = krt.plan_trim(conn, cap=3)
    assert plan2["affected_docs"] == 0
    conn.close()
