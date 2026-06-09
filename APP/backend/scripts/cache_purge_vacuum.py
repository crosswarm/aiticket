#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缓存过期清理 + ChromaDB VACUUM 维护脚本（P5 护栏）。

背景：`VectorStore.cleanup_expired()` 与 analysis_cache.json 此前【无任何调度调用方】，
过期分析缓存无限累积；query_cache 仅惰性过期、无批量清理；全仓无 VACUUM，
chroma.sqlite3 反复 delete/re-add 留下大量 freelist 死页（QCL KB 实测 87.8% 死页）。

本脚本分四步（每步独立 try/except，单步失败不影响其余）：
  A. chroma analysis_collection 删过期（expires_at < now）—— 在线可执行
  B. chroma query_cache 批量删过期（expire_at < now）—— 在线可执行
  C. data_cache/analysis_cache.json 按 cached_at 删超 N 天（默认 90d）—— flock 保护
  D. VACUUM 各 chroma.sqlite3 回收 freelist —— ⚠️ 需独占锁

【关键】VACUUM 需要对 SQLite 文件的独占锁；后端 uvicorn 在跑时会持有 chroma 连接，
导致 VACUUM 报 SQLITE_BUSY。因此：
  - 日常调度（后端在线）：A/B/C 做逻辑删除（这才是阻止『无限增长』的根因修复），
    D 用短 busy_timeout 尝试，BUSY 即跳过并打印提示（不报错）。
  - 一次性深度回收（已累积的死页）：须停服窗口执行 `--vacuum-only`（见 DEPLOYMENT/运维）。

用法：
  python scripts/cache_purge_vacuum.py            # 真实执行 A/B/C + 尝试 D
  python scripts/cache_purge_vacuum.py --dry-run  # 只统计不删除、不 VACUUM
  python scripts/cache_purge_vacuum.py --vacuum-only  # 仅 VACUUM（停服窗口用）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import fcntl  # POSIX only；Windows 上降级为不加锁
    _HAS_FCNTL = True
except Exception:  # pragma: no cover
    _HAS_FCNTL = False

# APP/backend（scripts 的上一级）；尊重 DEMO_RUNTIME_DIR 重定向（与 board_service_chroma 一致）
BACKEND = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("DEMO_RUNTIME_DIR") or str(BACKEND))
ANALYSIS_CACHE_KEEP_DAYS = int(os.environ.get("ANALYSIS_CACHE_KEEP_DAYS", "90"))
VACUUM_BUSY_TIMEOUT_MS = int(os.environ.get("VACUUM_BUSY_TIMEOUT_MS", "5000"))


def _log(msg: str) -> None:
    print(f"[cache-purge] {msg}", flush=True)


def _chroma_persist_dirs() -> list[Path]:
    """候选 chroma 持久化目录（覆盖 deployable BASE_DIR/chroma_db 与 internal APP/data/chroma/* 两种布局）。"""
    cands: list[Path] = []
    data_dir = os.environ.get("DATA_DIR")
    cands.append(BASE_DIR / "chroma_db")                      # deployable: board_service_chroma persist_dir
    if data_dir:
        cands.append(Path(data_dir) / "chroma")              # Docker DATA_DIR/chroma
    cands.append(BASE_DIR.parent / "data" / "chroma")        # internal: APP/data/chroma/{kb,ticket,...}
    cands.append(BASE_DIR / "data" / "reply_trainer" / "chroma")
    cands.append(BASE_DIR / "data_cache" / "chroma_db")
    # 去重 + 仅保留存在的
    seen, out = set(), []
    for c in cands:
        rc = c.resolve()
        if rc not in seen and rc.exists():
            seen.add(rc)
            out.append(rc)
    return out


def _find_chroma_sqlite() -> list[Path]:
    dbs: list[Path] = []
    for d in _chroma_persist_dirs():
        # 直接含 chroma.sqlite3，或其下一/二级子目录（kb/ ticket/ 等）
        dbs.extend(Path(d).glob("chroma.sqlite3"))
        dbs.extend(Path(d).glob("*/chroma.sqlite3"))
    # 兜底：从 BASE_DIR 向上一级 + DATA_DIR 全局搜（限两层，避免深扫）
    extra_roots = [BASE_DIR / "chroma_db", BASE_DIR.parent / "data"]
    dd = os.environ.get("DATA_DIR")
    if dd:
        extra_roots.append(Path(dd))
    for r in extra_roots:
        if r.exists():
            dbs.extend(Path(r).glob("**/chroma.sqlite3"))
    # 去重
    uniq, seen = [], set()
    for p in dbs:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(rp)
    return uniq


def step_a_b_chroma_logical(dry_run: bool) -> None:
    """A: analysis_collection 删过期；B: query_cache 批量删过期。需 VectorStore（在线可执行）。"""
    persist = BASE_DIR / "chroma_db"
    if not persist.exists():
        _log(f"[A/B] 跳过：未找到 chroma_db（{persist}）")
        return
    try:
        sys.path.insert(0, str(BACKEND))
        from vector_store import VectorStore  # noqa: E402
    except Exception as e:
        _log(f"[A/B] 跳过：导入 VectorStore 失败：{e!r}")
        return
    try:
        vs = VectorStore(persist_directory=str(persist), allow_download=False)
    except Exception as e:
        _log(f"[A/B] 跳过：VectorStore 实例化失败：{e!r}")
        return

    # A. analysis_collection 过期
    try:
        n = vs.cleanup_expired(dry_run=dry_run)
        _log(f"[A] analysis_cache(chroma) 过期 {'将删' if dry_run else '已删'} {n} 条")
    except Exception as e:
        _log(f"[A] analysis cleanup_expired 失败：{e!r}")

    # B. query_cache 批量删过期（expire_at < now）
    try:
        qc = getattr(vs, "query_cache", None)
        if qc is None:
            _log("[B] 跳过：query_cache 集合不可用")
        else:
            allm = qc.get(include=["metadatas"])
            ids = allm.get("ids", []) or []
            metas = allm.get("metadatas", []) or []
            now = datetime.now()
            expired = []
            for i, m in zip(ids, metas):
                try:
                    ea = datetime.fromisoformat((m or {}).get("expire_at", "2000-01-01"))
                except Exception:
                    ea = datetime(2000, 1, 1)
                if ea < now:
                    expired.append(i)
            if expired and not dry_run:
                qc.delete(ids=expired)
            _log(f"[B] query_cache 过期 {'将删' if dry_run else '已删'} {len(expired)} / 共 {len(ids)} 条")
    except Exception as e:
        _log(f"[B] query_cache 清理失败：{e!r}")


def step_c_analysis_json(dry_run: bool) -> None:
    """C: data_cache/analysis_cache.json 按 cached_at(ISO) 删超 KEEP_DAYS（flock + 原子写）。"""
    cache_file = BASE_DIR / "data_cache" / "analysis_cache.json"
    if not cache_file.exists():
        _log(f"[C] 跳过：未找到 {cache_file}")
        return
    cutoff = datetime.now() - timedelta(days=ANALYSIS_CACHE_KEEP_DAYS)

    def _parse(ts):
        if not ts:
            return None
        try:
            return datetime.fromisoformat(str(ts))
        except Exception:
            try:  # 兜底 epoch 秒
                return datetime.fromtimestamp(float(ts))
            except Exception:
                return None

    try:
        with open(cache_file, "r+", encoding="utf-8") as f:
            if _HAS_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                raw = f.read() or "{}"
                data = json.loads(raw)
                if not isinstance(data, dict):
                    _log("[C] 跳过：analysis_cache.json 非 dict 结构")
                    return
                before = len(data)
                kept = {}
                for k, v in data.items():
                    ts = (v or {}).get("cached_at") if isinstance(v, dict) else None
                    dt = _parse(ts)
                    # 无时间戳的条目保守保留（不误删）
                    if dt is None or dt >= cutoff:
                        kept[k] = v
                removed = before - len(kept)
                if removed > 0 and not dry_run:
                    f.seek(0)
                    json.dump(kept, f, ensure_ascii=False)
                    f.truncate()
                _log(f"[C] analysis_cache.json {'将删' if dry_run else '已删'} {removed} 条（{before}→{len(kept)}，保留<{ANALYSIS_CACHE_KEEP_DAYS}d）")
            finally:
                if _HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        _log(f"[C] analysis_cache.json 清理失败：{e!r}")


def step_d_vacuum(dry_run: bool) -> None:
    """D: VACUUM 各 chroma.sqlite3。后端在线时多半 SQLITE_BUSY → 跳过并提示（不报错）。"""
    dbs = _find_chroma_sqlite()
    if not dbs:
        _log("[D] 跳过：未发现任何 chroma.sqlite3")
        return
    for db in dbs:
        try:
            conn = sqlite3.connect(str(db), timeout=VACUUM_BUSY_TIMEOUT_MS / 1000.0)
            conn.execute(f"PRAGMA busy_timeout={VACUUM_BUSY_TIMEOUT_MS}")
            pc = conn.execute("PRAGMA page_count").fetchone()[0]
            fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
            ps = conn.execute("PRAGMA page_size").fetchone()[0]
            mb = pc * ps / (1024 * 1024)
            dead_pct = (fl / pc * 100) if pc else 0
            if dry_run:
                _log(f"[D] {db}：{mb:.1f}MB，死页 {fl}/{pc}（{dead_pct:.1f}%）[dry-run 不 VACUUM]")
                conn.close()
                continue
            try:
                conn.execute("VACUUM")
                pc2 = conn.execute("PRAGMA page_count").fetchone()[0]
                _log(f"[D] {db}：VACUUM 完成，{pc}→{pc2} 页（释放 {(pc-pc2)*ps/(1024*1024):.1f}MB，原死页 {dead_pct:.1f}%）")
            except sqlite3.OperationalError as ve:
                _log(f"[D] {db}：VACUUM 跳过（{ve}）—— 多半后端持锁(SQLITE_BUSY)，需停服窗口跑 --vacuum-only。死页 {dead_pct:.1f}%")
            conn.close()
        except Exception as e:
            _log(f"[D] {db}：检查/VACUUM 失败：{e!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="缓存过期清理 + chroma VACUUM")
    ap.add_argument("--dry-run", action="store_true", help="只统计不删除/不 VACUUM")
    ap.add_argument("--vacuum-only", action="store_true", help="仅跑 VACUUM（停服窗口用）")
    args = ap.parse_args()

    t0 = time.time()
    _log(f"开始：BASE_DIR={BASE_DIR} dry_run={args.dry_run} vacuum_only={args.vacuum_only} keep_days={ANALYSIS_CACHE_KEEP_DAYS}")
    if not args.vacuum_only:
        step_a_b_chroma_logical(args.dry_run)
        step_c_analysis_json(args.dry_run)
    step_d_vacuum(args.dry_run)
    _log(f"完成，耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
