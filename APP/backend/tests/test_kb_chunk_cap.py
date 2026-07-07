"""KB 单文档 chunk 上限：防大型转换文档(统计 xlsx/长 PDF)切出天量 chunk 撑爆库。

历史事故：一个『业务流SOP统计2024.xlsx』被铺平后切出 565383 chunk → kb_chunks.db 67G。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kb_hybrid_index as khi


def _idx():
    return khi.KnowledgeHybridIndex.__new__(khi.KnowledgeHybridIndex)


def test_chunk_cap_truncates_oversized_doc():
    text = "\n".join(f"line{i} " + "x" * 200 for i in range(10000))
    out = khi.KnowledgeHybridIndex._chunk_text(_idx(), text)
    assert len(out) == khi._MAX_CHUNKS_PER_DOC
    assert khi._MAX_CHUNKS_PER_DOC == 2000


def test_chunk_cap_leaves_small_doc_untouched():
    out = khi.KnowledgeHybridIndex._chunk_text(_idx(), "第一行\n第二行\n第三行")
    assert 0 < len(out) <= 3


def test_chunk_empty_text_returns_one():
    out = khi.KnowledgeHybridIndex._chunk_text(_idx(), "只有一句话")
    assert len(out) == 1
