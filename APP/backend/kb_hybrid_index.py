from __future__ import annotations

import math
import re
import shutil
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import chromadb
from chromadb.config import Settings


TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}")

_SOURCE_PRIOR = {
    'human_verified':   1.0,   # 2+人工回复验证 — 最高优先级（生产的内容就是未来的文档）
    'kb_compiled':      0.98,  # 编译综合页
    'apcom_docs':       0.95,
    'kb_local':         0.90,
    'user_contributed': 0.88,  # 实战经验
    'kb_auto_enriched': 0.75,  # 自动萃取入库的知识
    'reply_example':    0.70,
    'ticket_case':      0.60,
    'legacy_files':     0.40,
}

# sync/rebuild 时必须保留的 source_kind（增量写入、不在 manifest 里、清空后无法自动恢复）
_PRESERVED_SOURCE_KINDS: tuple[str, ...] = (
    'kb_compiled',
    'user_contributed',
    'kb_auto_enriched',
    'human_verified',
    'reply_example',
)

# 单文档 chunk 上限：防止大型转换文档(统计 xlsx/长 PDF 等)铺平成天量文本时切出
# 数十万 chunk 撑爆整库(历史事故: 一个统计表 565383 chunk → 67G 库)。超限截断 + 告警。
_MAX_CHUNKS_PER_DOC = 2000


class LocalHashEmbeddingFunction:
    """Deterministic local embedding to keep Chroma usable without downloads."""

    def __init__(self, dimensions: int = 96) -> None:
        self.dimensions = dimensions

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in input]

    def name(self) -> str:
        return "local-hash-embedding"

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return self.__call__(input)

    def embed_query(self, input: list[str] | str) -> list[list[float]] | list[float]:
        if isinstance(input, str):
            return self._embed(input)
        return self.__call__(input)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        counts = Counter(token.lower() for token in TOKEN_RE.findall(text or ""))
        if not counts:
            return vector
        for token, weight in counts.items():
            vector[hash(token) % self.dimensions] += float(weight)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


class KnowledgeHybridIndex:
    def __init__(self, sqlite_path: Path, chroma_path: Path, collection_name: str = "prd_kb") -> None:
        self.sqlite_path = Path(sqlite_path)
        self.chroma_path = Path(chroma_path)
        self.recovery_chroma_path = self.chroma_path.parent / f"{self.chroma_path.name}_runtime"
        self.collection_name = collection_name
        self.embedding_function = LocalHashEmbeddingFunction()
        self._lock = threading.RLock()

        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.chroma_path.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.sqlite_path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_sqlite()

        from services.chroma_factory import get_chroma_client
        self.client = get_chroma_client(persist_path=str(self.chroma_path))
        self.collection = self._create_collection_with_recovery()

    def rebuild(self, items: list[dict[str, Any]], text_loader: Callable[[dict[str, Any]], str]) -> dict[str, int]:
        """全量重建索引。fcntl flock 防止多进程同时 rebuild 损坏文件。

        返回 dict with keys: chunk_count, skipped_unchanged
        （向后兼容：调用方可继续用 int(result) 或 result.get("chunk_count")）
        """
        import fcntl
        _lock_path = str(self.sqlite_path) + ".rebuild.lock"
        with open(_lock_path, "w") as _flock_file:
            try:
                fcntl.flock(_flock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.warning("[KnowledgeHybridIndex] rebuild: 另一进程正在重建，跳过")
                return {"chunk_count": -1, "skipped_unchanged": 0}
            return self._rebuild_locked(items, text_loader)

    def _rebuild_locked(self, items: list[dict[str, Any]], text_loader: Callable[[dict[str, Any]], str]) -> dict[str, int]:
        with self._lock:
            # 重建前先备份受保护的 source_kind（不在 manifest 里，清空后无法自动恢复）
            preserved = self._dump_preserved_kinds()
            if preserved:
                print(f"[KnowledgeHybridIndex] rebuild: 保护 {len(preserved)} 条受保护数据（{set(p[0]['source_kind'] for p in preserved)}）")

            # 增量去重：在 reset 前先快照当前 (content_id → source_mtime) 映射
            _existing_mtime: dict[str, str | None] = {}
            try:
                rows = self.conn.execute("SELECT content_id, source_mtime FROM documents").fetchall()
                for r in rows:
                    _existing_mtime[r["content_id"]] = r["source_mtime"]
            except Exception:
                pass  # 表不存在或列缺失时退化为全量

            # 对 mtime 未变的条目，在 reset 前缓存其 chunk_text（reset 后用缓存代替磁盘读）
            _cached_text: dict[str, str] = {}
            for item in items:
                _mtime = item.get("source_mtime")
                if _mtime is not None and _existing_mtime.get(item["content_id"]) == _mtime:
                    try:
                        chunks = self.conn.execute(
                            "SELECT chunk_text FROM chunks WHERE content_id=? ORDER BY chunk_index",
                            (item["content_id"],)
                        ).fetchall()
                        _text = "\n".join(c["chunk_text"] for c in chunks)
                        if _text.strip():
                            _cached_text[item["content_id"]] = _text
                    except Exception:
                        pass  # 缓存失败时 reset 后 fallback 到 text_loader

            self._reset()
            chunk_count = 0
            skipped_unchanged = 0
            _batch_counter = 0
            _chroma_queue: list[tuple[list, list, list]] = []
            try:
                for item in items:
                    _item_mtime: str | None = item.get("source_mtime")

                    # 若 mtime 有效且缓存命中 → 用缓存文本，计入 skipped_unchanged
                    if _item_mtime is not None:
                        _prev = _existing_mtime.get(item["content_id"])
                        if _prev is not None and _prev == _item_mtime:
                            _ct = _cached_text.get(item["content_id"])
                            if _ct:
                                skipped_unchanged += 1
                                # 仍需写回 db（reset 已清空），使用缓存文本代替磁盘读
                                text = _ct
                            else:
                                text = text_loader(item).strip()  # fallback: cache miss
                        else:
                            text = text_loader(item).strip()
                    else:
                        text = text_loader(item).strip()
                    if not text:
                        continue

                    _pk = item.get("project_key", "_global") or "_global"
                    self.conn.execute(
                        """
                        INSERT INTO documents (
                            content_id, source_kind, name, summary, source_rel_path,
                            citation_label, l1_module, l2_module, doc_type, project_key,
                            source_mtime
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["content_id"],
                            item["source_kind"],
                            item.get("name", ""),
                            item.get("summary", ""),
                            item.get("source_rel_path", ""),
                            item.get("citation_label", ""),
                            item.get("l1_module", ""),
                            item.get("l2_module", ""),
                            item.get("doc_type", ""),
                            _pk,
                            _item_mtime,
                        ),
                    )

                    chroma_ids: list[str] = []
                    chroma_docs: list[str] = []
                    chroma_metas: list[dict[str, Any]] = []
                    for index, chunk in enumerate(self._chunk_text(text), start=1):
                        chunk_id = f"{item['content_id']}::chunk-{index:03d}"
                        self.conn.execute(
                            """
                            INSERT INTO chunks (
                                chunk_id, content_id, chunk_index, chunk_text, chunk_preview,
                                source_kind, name, summary, source_rel_path, citation_label,
                                l1_module, l2_module, doc_type, project_key
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_id,
                                item["content_id"],
                                index,
                                chunk,
                                chunk[:240],
                                item["source_kind"],
                                item.get("name", ""),
                                item.get("summary", ""),
                                item.get("source_rel_path", ""),
                                item.get("citation_label", ""),
                                item.get("l1_module", ""),
                                item.get("l2_module", ""),
                                item.get("doc_type", ""),
                                _pk,
                            ),
                        )
                        self.conn.execute(
                            """
                            INSERT INTO chunks_fts (
                                chunk_id, name, summary, keywords, source_rel_path, chunk_text
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_id,
                                item.get("name", ""),
                                item.get("summary", ""),
                                " ".join(item.get("keywords", [])),
                                item.get("source_rel_path", ""),
                                chunk,
                            ),
                        )
                        chroma_ids.append(chunk_id)
                        chroma_docs.append(chunk)
                        chroma_metas.append(
                            {
                                "content_id": item["content_id"],
                                "source_kind": item["source_kind"],
                                "name": item.get("name", ""),
                                "summary": item.get("summary", "")[:500],
                                "source_rel_path": item.get("source_rel_path", ""),
                                "citation_label": item.get("citation_label", ""),
                                "l1_module": item.get("l1_module", ""),
                                "l2_module": item.get("l2_module", ""),
                                "doc_type": item.get("doc_type", ""),
                                "project_key": _pk,
                            }
                        )
                        chunk_count += 1

                    if chroma_ids:
                        _chroma_queue.append((chroma_ids, chroma_docs, chroma_metas))

                    _batch_counter += 1
                    if _batch_counter % 50 == 0:
                        self.conn.commit()

                # Final commit for remaining items
                self.conn.commit()
            finally:
                # 无论 rebuild 成功还是异常，始终恢复受保护数据
                # 保证 sync 失败时 kb_compiled 等增量数据不丢失
                for p_item, p_text in preserved:
                    try:
                        added = self.add_item(p_item, p_text)
                        if added > 0:
                            chunk_count += added
                    except Exception as e:
                        print(f"[KnowledgeHybridIndex] 恢复受保护条目失败 {p_item.get('content_id')}: {e}")

            # Flush ChromaDB in background — SQLite already committed and readable
            _col = self.collection
            _q = _chroma_queue
            def _chroma_flush(q=_q, col=_col):
                for ids, docs, metas in q:
                    try:
                        col.add(ids=ids, documents=docs, metadatas=metas)
                    except Exception as e:
                        print(f"[rebuild] chroma flush: {e}")
            threading.Thread(target=_chroma_flush, daemon=True).start()

            if skipped_unchanged:
                print(f"[KnowledgeHybridIndex] rebuild: 跳过未变化 {skipped_unchanged} 条（mtime 相同）")
            return {"chunk_count": chunk_count, "skipped_unchanged": skipped_unchanged}

    def _dump_preserved_kinds(self) -> list[tuple[dict[str, Any], str]]:
        """导出所有受保护 source_kind 的文档（含完整 chunk 文本），用于 rebuild 前暂存。"""
        if not _PRESERVED_SOURCE_KINDS:
            return []
        placeholders = ",".join("?" * len(_PRESERVED_SOURCE_KINDS))
        rows = self.conn.execute(
            f"""SELECT content_id, source_kind, name, summary, source_rel_path,
                       citation_label, l1_module, l2_module, doc_type
                FROM documents WHERE source_kind IN ({placeholders})""",
            _PRESERVED_SOURCE_KINDS,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            chunks = self.conn.execute(
                "SELECT chunk_text FROM chunks WHERE content_id = ? ORDER BY chunk_index",
                (row["content_id"],),
            ).fetchall()
            full_text = "\n".join(c["chunk_text"] for c in chunks)
            if full_text.strip():
                result.append((item, full_text))
        return result

    def list_by_source_kinds(self, source_kinds: tuple[str, ...], top_k: int = 500) -> list[dict[str, Any]]:
        """按多个 source_kind 列出文档（含完整 content）。"""
        if not source_kinds:
            return []
        placeholders = ",".join("?" * len(source_kinds))
        rows = self.conn.execute(
            f"""SELECT content_id, source_kind, name, summary, source_rel_path,
                       citation_label, l1_module, l2_module, doc_type
                FROM documents WHERE source_kind IN ({placeholders}) LIMIT ?""",
            (*source_kinds, top_k),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            chunks = self.conn.execute(
                "SELECT chunk_text FROM chunks WHERE content_id = ? ORDER BY chunk_index",
                (row["content_id"],),
            ).fetchall()
            d["content"] = "\n".join(c["chunk_text"] for c in chunks)
            result.append(d)
        return result

    def count_by_source_kind(self, source_kind: str) -> int:
        """返回指定 source_kind 的文档数量。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source_kind = ?", (source_kind,)
        ).fetchone()
        return row[0] if row else 0

    def delete_item(self, content_id: str) -> bool:
        """删除单条文档及其所有 chunks（同步删除 Chroma）。返回是否存在并被删除。"""
        with self._lock:
            existing = self.conn.execute(
                "SELECT 1 FROM documents WHERE content_id = ?", (content_id,)
            ).fetchone()
            if not existing:
                return False
            old_ids = [
                row[0] for row in self.conn.execute(
                    "SELECT chunk_id FROM chunks WHERE content_id = ?", (content_id,)
                ).fetchall()
            ]
            if old_ids:
                try:
                    self.collection.delete(ids=old_ids)
                except Exception:
                    pass
                placeholders = ",".join("?" * len(old_ids))
                self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
                self.conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", old_ids)
            self.conn.execute("DELETE FROM documents WHERE content_id = ?", (content_id,))
            self.conn.commit()
            return True

    def add_item(self, item: dict[str, Any], text: str, source_mtime: str | None = None) -> int:
        """增量插入单条文档（支持 upsert：content_id 冲突时先删旧数据再插入）。

        source_mtime: 文件修改时间字符串（ISO 8601 / stat mtime）。
            若提供且与已存 mtime 相同，则跳过本次写入（返回 -1 表示 skipped）。
            为 None 时行为与旧版一致（不跳过）。
        """
        text = text.strip()
        if not text:
            return 0
        content_id = item["content_id"]
        with self._lock:
            # 若提供 mtime，检查是否已有相同记录且 mtime 未变
            if source_mtime is not None:
                existing_row = self.conn.execute(
                    "SELECT source_mtime FROM documents WHERE content_id = ?", (content_id,)
                ).fetchone()
                if existing_row is not None and existing_row["source_mtime"] == source_mtime:
                    return -1  # skipped — content unchanged

            # upsert: 先删除已有记录
            existing = self.conn.execute(
                "SELECT 1 FROM documents WHERE content_id = ?", (content_id,)
            ).fetchone()
            if existing:
                old_ids = [
                    row[0] for row in self.conn.execute(
                        "SELECT chunk_id FROM chunks WHERE content_id = ?", (content_id,)
                    ).fetchall()
                ]
                if old_ids:
                    try:
                        self.collection.delete(ids=old_ids)
                    except Exception:
                        pass
                    placeholders = ",".join("?" * len(old_ids))
                    self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
                    self.conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", old_ids)
                self.conn.execute("DELETE FROM documents WHERE content_id = ?", (content_id,))

            _pk = item.get("project_key", "_global") or "_global"
            # 插入 documents（含 source_mtime）
            self.conn.execute(
                """INSERT INTO documents (content_id, source_kind, name, summary,
                   source_rel_path, citation_label, l1_module, l2_module, doc_type, project_key,
                   source_mtime)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content_id, item["source_kind"], item.get("name", ""),
                 item.get("summary", ""), item.get("source_rel_path", ""),
                 item.get("citation_label", ""), item.get("l1_module", ""),
                 item.get("l2_module", ""), item.get("doc_type", ""), _pk,
                 source_mtime),
            )

            # 分块 → 插入 chunks + fts + chroma
            chroma_ids, chroma_docs, chroma_metas = [], [], []
            chunk_count = 0
            for index, chunk in enumerate(self._chunk_text(text), start=1):
                chunk_id = f"{content_id}::chunk-{index:03d}"
                self.conn.execute(
                    """INSERT INTO chunks (chunk_id, content_id, chunk_index, chunk_text,
                       chunk_preview, source_kind, name, summary, source_rel_path,
                       citation_label, l1_module, l2_module, doc_type, project_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chunk_id, content_id, index, chunk, chunk[:240],
                     item["source_kind"], item.get("name", ""),
                     item.get("summary", ""), item.get("source_rel_path", ""),
                     item.get("citation_label", ""), item.get("l1_module", ""),
                     item.get("l2_module", ""), item.get("doc_type", ""), _pk),
                )
                self.conn.execute(
                    """INSERT INTO chunks_fts (chunk_id, name, summary, keywords,
                       source_rel_path, chunk_text) VALUES (?, ?, ?, ?, ?, ?)""",
                    (chunk_id, item.get("name", ""), item.get("summary", ""),
                     " ".join(item.get("keywords", [])),
                     item.get("source_rel_path", ""), chunk),
                )
                chroma_ids.append(chunk_id)
                chroma_docs.append(chunk)
                chroma_metas.append({
                    "content_id": content_id,
                    "source_kind": item["source_kind"],
                    "name": item.get("name", ""),
                    "summary": item.get("summary", "")[:500],
                    "source_rel_path": item.get("source_rel_path", ""),
                    "citation_label": item.get("citation_label", ""),
                    "l1_module": item.get("l1_module", ""),
                    "l2_module": item.get("l2_module", ""),
                    "doc_type": item.get("doc_type", ""),
                    "project_key": _pk,
                })
                chunk_count += 1

            # Commit SQLite first — SQLite is source of truth; ChromaDB is best-effort
            self.conn.commit()

            if chroma_ids:
                try:
                    self.collection.add(ids=chroma_ids, documents=chroma_docs, metadatas=chroma_metas)
                except Exception as _chroma_err:
                    logger.warning("[add_item] ChromaDB add failed (SQLite committed OK): %s", _chroma_err)

            return chunk_count

    def search(self, query: str, top_k: int = 20, source_kind: str | None = None, category: str | None = None, project_key: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            fts_hits = self._search_fts(query, top_k=top_k * 3, source_kind=source_kind, category=category, project_key=project_key)
            vector_hits = self._search_vector(query, top_k=top_k * 3, source_kind=source_kind, category=category, project_key=project_key)

            combined: dict[str, dict[str, Any]] = {}
            for hit in fts_hits:
                row = combined.setdefault(hit["chunk_id"], {**hit, "fts_score": 0.0, "vector_score": 0.0})
                row["fts_score"] = max(row["fts_score"], hit["fts_score"])

            for hit in vector_hits:
                row = combined.setdefault(hit["chunk_id"], {**hit, "fts_score": 0.0, "vector_score": 0.0})
                row["vector_score"] = max(row["vector_score"], hit["vector_score"])

            for item in combined.values():
                source_prior = _SOURCE_PRIOR.get(item["source_kind"], 0.85)
                # FTS(BM25)是中文搜索最可靠的方式，hash向量仅做补充
                item["score"] = round(item["vector_score"] * 0.25 + item["fts_score"] * 0.55 + source_prior * 0.20, 4)
                item["match_type"] = "chunk"

            return sorted(combined.values(), key=lambda item: (-item["score"], item["name"]))[:top_k]

    def list_by_source_kind(self, source_kind: str, top_k: int = 100) -> list[dict[str, Any]]:
        """直接按 source_kind 列出所有文档（含拼接后的完整 content），不依赖搜索查询。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT content_id, source_kind, name, summary, source_rel_path,
                          citation_label, l1_module, l2_module, doc_type
                   FROM documents WHERE source_kind = ? LIMIT ?""",
                (source_kind, top_k),
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                chunks = self.conn.execute(
                    "SELECT chunk_text FROM chunks WHERE content_id = ? ORDER BY chunk_index",
                    (row["content_id"],),
                ).fetchall()
                d["content"] = "\n".join(c["chunk_text"] for c in chunks)
                results.append(d)
            return results

    def get_chunks_for_content(self, content_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT chunk_id, chunk_index, chunk_preview, chunk_text
                FROM chunks
                WHERE content_id = ?
                ORDER BY chunk_index
                LIMIT ?
                """,
                (content_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_chunks(self) -> int:
        with self._lock:
            cursor = self.conn.execute("SELECT COUNT(*) FROM chunks")
            return int(cursor.fetchone()[0])

    def _init_sqlite(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    content_id TEXT PRIMARY KEY,
                    source_kind TEXT,
                    name TEXT,
                    summary TEXT,
                    source_rel_path TEXT,
                    citation_label TEXT,
                    l1_module TEXT,
                    l2_module TEXT,
                    doc_type TEXT,
                    project_key TEXT DEFAULT '_global',
                    source_mtime TEXT,
                    credibility REAL DEFAULT 0.8,
                    validation_sources TEXT,
                    last_validated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    content_id TEXT,
                    chunk_index INTEGER,
                    chunk_text TEXT,
                    chunk_preview TEXT,
                    source_kind TEXT,
                    name TEXT,
                    summary TEXT,
                    source_rel_path TEXT,
                    citation_label TEXT,
                    l1_module TEXT,
                    l2_module TEXT,
                    doc_type TEXT,
                    project_key TEXT DEFAULT '_global'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    name,
                    summary,
                    keywords,
                    source_rel_path,
                    chunk_text
                );

                CREATE TABLE IF NOT EXISTS pending_kb_questions (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    source_compiled_id TEXT,
                    options_json TEXT NOT NULL,
                    default_choice TEXT NOT NULL DEFAULT 'C',
                    llm_confidence REAL,
                    llm_reasoning TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    jobmaster_decision_id TEXT,
                    created_at TEXT,
                    asked_at TEXT,
                    resolved_at TEXT,
                    resolved_choice TEXT,
                    resolved_by TEXT
                );
                """
            )
            self.conn.commit()
            # Migration: add project_key column to existing tables
            for tbl in ("documents", "chunks"):
                try:
                    self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN project_key TEXT DEFAULT '_global'")
                    self.conn.commit()
                except Exception:
                    pass  # column already exists
            # Migration: add source_mtime column to documents (Layer2-1)
            try:
                self.conn.execute("ALTER TABLE documents ADD COLUMN source_mtime TEXT")
                self.conn.commit()
            except Exception:
                pass  # column already exists
            # Migration: add enricher columns to documents
            for col, defn in [
                ("credibility", "REAL DEFAULT 0.8"),
                ("validation_sources", "TEXT"),
                ("last_validated_at", "TEXT"),
            ]:
                try:
                    self.conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {defn}")
                    self.conn.commit()
                except Exception:
                    pass  # column already exists
            # Index for fast content_id lookups (avoids full table scan on 270K chunks)
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_content_id ON chunks(content_id)"
            )
            self.conn.commit()

    def _reset(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM documents")
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM chunks_fts")
            self.conn.commit()

            try:
                self.client.delete_collection(self.collection_name)
            except Exception:
                pass
            self.collection = self._create_collection_with_recovery()

    def _chunk_text(self, text: str, max_chars: int = 900, overlap: int = 120) -> list[str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        sections: list[str] = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 <= max_chars:
                current = f"{current}\n{line}".strip()
                continue
            if current:
                sections.append(current)
            current = f"{current[-overlap:]}\n{line}".strip() if current else line
        if current:
            sections.append(current)
        sections = sections or [text[:max_chars]]
        if len(sections) > _MAX_CHUNKS_PER_DOC:
            print(f"[KBIndex] ⚠️ 单文档切出 {len(sections)} chunk 超上限 {_MAX_CHUNKS_PER_DOC}，截断（疑大型转换文档/统计表）")
            sections = sections[:_MAX_CHUNKS_PER_DOC]
        return sections

    def _search_fts(self, query: str, top_k: int, source_kind: str | None, category: str | None, project_key: str | None = None) -> list[dict[str, Any]]:
        tokens = [token for token in TOKEN_RE.findall(query or "") if token]
        if not tokens:
            return []
        # 双引号包裹 token 防止 FTS5 把 "-" 等符号当操作符
        match_query = " OR ".join(f'"{t}"' for t in tokens)
        where = []
        params: list[Any] = [match_query]
        if source_kind:
            where.append("c.source_kind = ?")
            params.append(source_kind)
        if category:
            where.append("(c.l1_module = ? OR c.l2_module = ?)")
            params.extend([category, category])
        if project_key:
            where.append("(c.project_key = ? OR c.project_key = '_global')")
            params.append(project_key)
        where_sql = f" AND {' AND '.join(where)}" if where else ""
        sql = f"""
            SELECT
                c.chunk_id, c.content_id, c.chunk_index, c.chunk_preview, c.chunk_text,
                c.source_kind, c.name, c.summary, c.source_rel_path, c.citation_label,
                c.l1_module, c.l2_module, c.doc_type, c.project_key,
                bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? {where_sql}
            ORDER BY rank
            LIMIT ?
        """
        params.append(top_k)
        rows = []
        for row in self.conn.execute(sql, params).fetchall():
            rank = abs(float(row["rank"]))
            rows.append({**dict(row), "fts_score": 1.0 / (1.0 + rank)})
        return rows

    def _search_vector(self, query: str, top_k: int, source_kind: str | None, category: str | None, project_key: str | None = None) -> list[dict[str, Any]]:
        try:
            if self.collection.count() == 0:
                return []
        except Exception as _chroma_err:
            logger.warning("[_search_vector] ChromaDB collection unavailable, falling back to FTS only: %s", _chroma_err)
            return []
        where = {"source_kind": source_kind} if source_kind else None
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=min(max(top_k, 1), self.collection.count()),
                where=where,
                include=["metadatas", "distances", "documents"],
            )
        except Exception as _chroma_err:
            logger.warning("[_search_vector] ChromaDB query failed, falling back to FTS only: %s", _chroma_err)
            return []

        hits: list[dict[str, Any]] = []
        ids = results.get("ids", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        documents = results.get("documents", [[]])[0]
        for chunk_id, metadata, distance, document in zip(ids, metadatas, distances, documents):
            if category and category not in {metadata.get("l1_module"), metadata.get("l2_module")}:
                continue
            if project_key and metadata.get("project_key", "_global") not in (project_key, "_global"):
                continue
            hits.append(
                {
                    "chunk_id": chunk_id,
                    "content_id": metadata.get("content_id", ""),
                    "chunk_index": self._chunk_index_from_id(chunk_id),
                    "chunk_preview": (document or "")[:240],
                    "chunk_text": document or "",
                    "source_kind": metadata.get("source_kind", ""),
                    "name": metadata.get("name", ""),
                    "summary": metadata.get("summary", ""),
                    "source_rel_path": metadata.get("source_rel_path", ""),
                    "citation_label": metadata.get("citation_label", ""),
                    "l1_module": metadata.get("l1_module", ""),
                    "l2_module": metadata.get("l2_module", ""),
                    "doc_type": metadata.get("doc_type", ""),
                    "project_key": metadata.get("project_key", "_global"),
                    "vector_score": max(0.0, 1.0 - float(distance)),
                }
            )
        return hits

    def _chunk_index_from_id(self, chunk_id: str) -> int:
        match = re.search(r"chunk-(\d+)$", chunk_id)
        return int(match.group(1)) if match else 0

    def _create_collection_with_recovery(self):
        try:
            return self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self.embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            # Chroma 版本切换后，旧 collection config 可能无法反序列化。
            shutil.rmtree(self.recovery_chroma_path, ignore_errors=True)
            self.recovery_chroma_path.mkdir(parents=True, exist_ok=True)
            from services.chroma_factory import get_chroma_client
            self.client = get_chroma_client(persist_path=str(self.recovery_chroma_path))
            return self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self.embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
