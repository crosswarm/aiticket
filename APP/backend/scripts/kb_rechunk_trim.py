#!/usr/bin/env python3
"""kb_rechunk_trim — 清理 KB 过度切分：把单文档 chunk 数压到 `--cap` 上限以内。

背景 / 根因（2026-07 实证，详见 kb_hybrid_index.py:_MAX_CHUNKS_PER_DOC 注释）:
  kb_chunks.db 被极度过切——少数「超大转换文档」(手册/红皮书/培训材料，正文达 ~176 万
  字符/篇) 被切成 2000 片顶满旧上限，占了全库六成以上 chunk；对应 chroma/kb 巨库让长驻
  daemon 反复载入内存导致泄漏。实测每个 chunk 平均 ~880 字符、99.9% >=700 字符，即 chunk
  已切到 max_chars=900 的目标粒度——根因是「文档超大」而非「切得太碎」。且 serving 嵌入
  bge-base-zh-v1.5 的 max_seq_length=512 token(~500 中文字符) 已小于 900，再调大 chunk 粒度
  只会丢向量尾巴。故唯一正确杠杆是压低单文档上限。

为什么是「trim(截尾删除)」而不是「re-embed 重切」:
  既然被保留的前 cap 个 chunk 本就已经是 900 字符的目标粒度、用同样 max_chars 重切得到的就是
  同一批 chunk，"用新参数重新切分" 对它们等价于「保留前 cap 个、删掉超出的尾巴」。因此本脚本
  对过切文档只做 DELETE(chunks + chunks_fts + 对应 chroma 向量)，不重新 embed——更安全、更省
  内存、天然幂等。(若将来有人改动 max_chars 本身导致 chunk 边界变化，那属于「全量从源文件
  重建索引」的范畴，应走 kb-refresh-full 管线，而不是本 trim 脚本。)

安全前提（务必遵守）:
  * 本脚本必须在 KB 写入 daemon **暂停之后** 独占运行。apply 会以读写方式打开 kb_chunks.db，
    与活着的 jobmaster/后端并发写会抢锁甚至写坏。正式步骤见文件末尾 RUNBOOK。
  * 默认 `--dry-run`：只读统计、零写入。必须显式 `--apply` 才会改数据，且 apply 默认先备份。
  * 只读连接一律 `file:...?mode=ro`（不要 immutable=1——主库正被 checkpoint 时会读到 torn page
    报 "database disk image is malformed"）。

用法:
  # 只读预演(默认)——统计将删多少、预计库体积变化，绝不写入:
  HF_HUB_OFFLINE=1 python APP/backend/scripts/kb_rechunk_trim.py --dry-run --cap 300

  # 正式执行(先备份 → 截尾 → VACUUM)，需 daemon 已停:
  HF_HUB_OFFLINE=1 python APP/backend/scripts/kb_rechunk_trim.py --apply --cap 300 \
      --backup-dir /Volumes/MacMini/kb_trim_backup

  # 单独回收 chroma 孤儿 segment 目录(约 15G，与 trim 独立；同样需 daemon 已停):
  HF_HUB_OFFLINE=1 python APP/backend/scripts/kb_rechunk_trim.py --prune-chroma-orphans --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

# ---- 路径解析（与 kb_runtime_service / ingest_kb_sqlite_only 一致）----------------
_SCRIPTS_DIR = Path(__file__).resolve().parent            # APP/backend/scripts
_BACKEND_DIR = _SCRIPTS_DIR.parent                        # APP/backend
_PROJECT_ROOT = _BACKEND_DIR.parent.parent                # repo root
_DATA_ROOT = Path(os.environ.get("AITICKET_DATA_ROOT") or str(_PROJECT_ROOT / "APP" / "data"))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

DEFAULT_DB = _DATA_ROOT / "sqlite" / "kb_chunks.db"
DEFAULT_CHROMA = _DATA_ROOT / "chroma" / "kb"

# 默认 cap：与 kb_hybrid_index._MAX_CHUNKS_PER_DOC 保持同源（回退 300）。
try:
    from kb_hybrid_index import _MAX_CHUNKS_PER_DOC as _DEFAULT_CAP  # type: ignore
except Exception:
    _DEFAULT_CAP = 300


# ---- 只读连接 ------------------------------------------------------------------
def open_ro(db_path: Path) -> sqlite3.Connection:
    """只读打开(mode=ro，尊重 WAL 与锁，不写不抢写锁)。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def open_rw(db_path: Path) -> sqlite3.Connection:
    """读写打开(仅在 --apply 且 daemon 已停时使用)。"""
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


# ---- 规划（只读、可测）---------------------------------------------------------
def plan_trim(conn: sqlite3.Connection, cap: int) -> dict[str, Any]:
    """扫描每文档 chunk 数，返回将被 trim 的文档清单与汇总(不写任何数据)。"""
    rows = conn.execute(
        "SELECT content_id, COUNT(*) AS n FROM chunks GROUP BY content_id"
    ).fetchall()
    total_docs = len(rows)
    total_chunks = sum(r["n"] for r in rows)
    over = [(r["content_id"], r["n"]) for r in rows if r["n"] > cap]
    over.sort(key=lambda x: -x[1])
    to_delete = sum(n - cap for _, n in over)
    post_chunks = total_chunks - to_delete
    return {
        "cap": cap,
        "total_docs": total_docs,
        "total_chunks": total_chunks,
        "affected_docs": len(over),
        "to_delete": to_delete,
        "post_chunks": post_chunks,
        "over": over,  # list[(content_id, n)]
    }


def _batched(seq: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ---- trim 核心（可测：chroma 删除以回调注入）-----------------------------------
def trim_doc(
    conn: sqlite3.Connection,
    content_id: str,
    cap: int,
    chroma_delete: Callable[[list[str]], None] | None = None,
    id_batch: int = 500,
) -> int:
    """把单个文档截尾到 cap 片：删除 chunk_index>cap 的 chunk + fts + chroma 向量。

    幂等：文档已 <=cap 时返回 0、不动数据。返回删除的 chunk 数。
    内存安全：只取「本文档要删的 chunk_id」(至多 n-cap 个)，绝不整库载入。
    """
    ids = [
        r["chunk_id"]
        for r in conn.execute(
            "SELECT chunk_id FROM chunks WHERE content_id=? AND chunk_index>? ORDER BY chunk_index",
            (content_id, cap),
        ).fetchall()
    ]
    if not ids:
        return 0
    for batch in _batched(ids, id_batch):
        placeholders = ",".join("?" * len(batch))
        conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", batch)
        conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", batch)
        if chroma_delete is not None:
            try:
                chroma_delete(batch)
            except Exception as e:  # chroma 尽力而为，不阻断 sqlite（sqlite 是真相源）
                print(f"[trim] chroma delete 失败(content_id={content_id}): {e}")
    conn.commit()
    return len(ids)


# ---- chroma 客户端（delete-by-id 不需要嵌入模型，用本地 EF 避免加载 bge）--------
def _get_kb_collection(chroma_path: Path):
    """返回 serving KB collection(prd_kb_v2)。delete 不 embed，故不加载 bge，避免占内存。"""
    from services.chroma_factory import get_chroma_client
    from kb_hybrid_index import LocalHashEmbeddingFunction

    client = get_chroma_client(persist_path=str(chroma_path))
    # deployable 的 KnowledgeHybridIndex 用固定 collection_name="prd_kb"(无版本后缀解析)。
    name = "prd_kb"
    try:
        return client, client.get_collection(name=name)
    except Exception:
        # 某些 chroma 版本 get_collection 也要 EF；用本地 hash EF(零下载/零显存)
        return client, client.get_or_create_collection(
            name=name,
            embedding_function=LocalHashEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )


# ---- 备份 ----------------------------------------------------------------------
def backup_sqlite(db_path: Path, backup_dir: Path) -> Path:
    """在线一致备份 kb_chunks.db(sqlite backup API，含 WAL 内容)。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"kb_chunks.db.bak"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def backup_chroma_meta(chroma_path: Path, backup_dir: Path) -> Path | None:
    """备份 chroma.sqlite3 元数据 + 记录当前 segment 目录清单(用于回滚参照)。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    meta = chroma_path / "chroma.sqlite3"
    if meta.exists():
        dest = backup_dir / "chroma.sqlite3.bak"
        shutil.copy2(meta, dest)
        listing = backup_dir / "chroma_kb_dirs_before.txt"
        dirs = sorted(p.name for p in chroma_path.iterdir() if p.is_dir())
        listing.write_text("\n".join(dirs), encoding="utf-8")
        return dest
    return None


# ---- chroma 孤儿 segment 目录回收（15G 大头，与 trim 独立）----------------------
def find_chroma_orphans(chroma_path: Path) -> tuple[list[str], list[str]]:
    """返回 (磁盘上所有 UUID 目录, 未被 segments 表引用的孤儿目录)。只读。"""
    meta = chroma_path / "chroma.sqlite3"
    live: set[str] = set()
    if meta.exists():
        c = sqlite3.connect(f"file:{meta}?mode=ro", uri=True, timeout=30.0)
        try:
            for r in c.execute("SELECT id FROM segments"):
                live.add(r[0])
        finally:
            c.close()
    dirs = [p.name for p in chroma_path.iterdir() if p.is_dir() and "-" in p.name]
    orphans = [d for d in dirs if d not in live]
    return dirs, orphans


def prune_chroma_orphans(chroma_path: Path, apply: bool, backup_dir: Path | None) -> dict[str, Any]:
    """删除未被 segments 表引用的 chroma 段目录(dead HNSW segments)。apply=False 仅统计。"""
    dirs, orphans = find_chroma_orphans(chroma_path)
    total_mb = 0
    for d in orphans:
        p = chroma_path / d
        try:
            total_mb += sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) // (1024 * 1024)
        except Exception:
            pass
    result = {"total_dirs": len(dirs), "orphans": len(orphans), "reclaim_mb": total_mb, "deleted": 0}
    if apply and orphans:
        if backup_dir is not None:
            backup_chroma_meta(chroma_path, backup_dir)
        for d in orphans:
            shutil.rmtree(chroma_path / d, ignore_errors=True)
            result["deleted"] += 1
    return result


# ---- 报告 ----------------------------------------------------------------------
def _fmt(n: int) -> str:
    return f"{n:,}"


def print_plan(plan: dict[str, Any], db_path: Path, chroma_path: Path, show_top: int = 20) -> None:
    tot = plan["total_chunks"]
    print("=" * 72)
    print(f"[dry-run] KB rechunk-trim 预演 (cap={plan['cap']})  —  零写入")
    print("=" * 72)
    print(f"  DB              : {db_path}")
    print(f"  当前文档数      : {_fmt(plan['total_docs'])}")
    print(f"  当前 chunk 总数 : {_fmt(tot)}")
    print(f"  受影响文档(>cap): {_fmt(plan['affected_docs'])}")
    print(f"  将删除 chunk    : {_fmt(plan['to_delete'])} ({100*plan['to_delete']/max(tot,1):.1f}%)")
    print(f"  trim 后 chunk   : {_fmt(plan['post_chunks'])}")
    # 体积粗估：sqlite 体积与 chunk 数近似正比(每 chunk 约含 chunk_text + FTS 副本 + 元数据)
    try:
        cur_bytes = (db_path).stat().st_size
        est_after = int(cur_bytes * plan["post_chunks"] / max(tot, 1))
        print(f"  sqlite 体积     : {cur_bytes/1e9:.1f} GB → VACUUM 后约 {est_after/1e9:.1f} GB (粗估)")
    except Exception:
        pass
    # chroma 孤儿概览(独立于 trim 的一次性大回收)
    try:
        dirs, orphans = find_chroma_orphans(chroma_path)
        print(f"  chroma/kb 目录  : {len(dirs)} 个 UUID 段目录，其中孤儿(可回收) {len(orphans)} 个")
        print(f"                    → 建议 --prune-chroma-orphans 单独回收(约 15G，见 RUNBOOK)")
    except Exception:
        pass
    print("-" * 72)
    print(f"  TOP {show_top} 过切文档(content_id | 当前 chunk | 将删):")
    for cid, n in plan["over"][:show_top]:
        print(f"    {str(cid)[:44]:<44} {n:>6} → 删 {n - plan['cap']:>6}")
    print("=" * 72)
    print("  这是 DRY-RUN，未写入任何数据。加 --apply 才执行(执行前会自动备份)。")


# ---- apply 编排 ----------------------------------------------------------------
def _load_progress(path: Path) -> set[str]:
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_progress(path: Path, done: set[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(done), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def run_apply(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    chroma_path = Path(args.chroma)
    cap = args.cap
    backup_dir = Path(args.backup_dir) if args.backup_dir else (
        db_path.parent / f"kb_trim_backup_{time.strftime('%Y%m%d_%H%M%S')}"
    )

    # 1) 预飞行安全检查
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0 and not args.force:
        w0 = wal.stat().st_size
        time.sleep(2.0)
        w1 = wal.stat().st_size if wal.exists() else 0
        if w1 != w0:
            print(f"[abort] 检测到 WAL 正在变化({w0}→{w1} bytes)——daemon 疑似仍在写。")
            print("        请先暂停 kb-refresh 调度并停止 jobmaster daemon(见 RUNBOOK)，"
                  "或确认无误后加 --force。")
            return 2

    # 2) 备份(默认开)
    if not args.skip_backup:
        print(f"[backup] 备份 sqlite → {backup_dir} (9G 量级，需数分钟/足够磁盘)")
        b = backup_sqlite(db_path, backup_dir)
        print(f"[backup] sqlite 备份完成: {b} ({b.stat().st_size/1e9:.1f} GB)")
        cb = backup_chroma_meta(chroma_path, backup_dir)
        if cb:
            print(f"[backup] chroma.sqlite3 备份完成: {cb}")
    else:
        print("[backup] 已跳过备份(--skip-backup)——高风险，仅当已在外层做过快照时使用。")

    # 3) 规划(只读)
    ro = open_ro(db_path)
    try:
        plan = plan_trim(ro, cap)
    finally:
        ro.close()
    print(f"[apply] cap={cap} 受影响文档={plan['affected_docs']} 将删 chunk={_fmt(plan['to_delete'])} "
          f"({_fmt(plan['total_chunks'])}→{_fmt(plan['post_chunks'])})")
    if plan["affected_docs"] == 0:
        print("[apply] 无过切文档，已达标，跳过。")
        return 0

    # 4) chroma collection(可选)
    chroma_delete = None
    client = None
    if not args.no_chroma:
        try:
            client, col = _get_kb_collection(chroma_path)

            def chroma_delete(ids: list[str], _col=col):
                _col.delete(ids=ids)

            print(f"[apply] chroma collection 就绪(delete-by-id，不加载 bge)。")
        except Exception as e:
            print(f"[apply] chroma 打开失败，仅处理 sqlite(chroma 稍后可用 repair_chroma 重建): {e}")

    # 5) 断点续跑
    progress_path = Path(args.progress) if args.progress else (backup_dir / "trim_progress.json")
    done = _load_progress(progress_path)
    if done:
        print(f"[apply] 断点续跑：已完成 {len(done)} 个文档，跳过之。")

    # 6) 逐文档 trim(流式、分 batch、每文档提交)
    rw = open_rw(db_path)
    deleted_total = 0
    processed = 0
    try:
        for cid, n in plan["over"]:
            if cid in done:
                continue
            d = trim_doc(rw, cid, cap, chroma_delete=chroma_delete, id_batch=args.batch)
            deleted_total += d
            processed += 1
            done.add(cid)
            if processed % 10 == 0:
                _save_progress(progress_path, done)
                print(f"[apply] 进度 {processed}/{plan['affected_docs']} 文档，累计删 {_fmt(deleted_total)} chunk")
        _save_progress(progress_path, done)
    finally:
        rw.close()
    print(f"[apply] trim 完成：{processed} 文档，删除 {_fmt(deleted_total)} chunk。")

    # 7) VACUUM(独占，回收物理空间)
    if not args.no_vacuum:
        print("[apply] VACUUM sqlite(独占，9G→数分钟)...")
        vc = open_rw(db_path)
        try:
            vc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            vc.execute("VACUUM")
            vc.commit()
        finally:
            vc.close()
        print(f"[apply] VACUUM 完成，当前体积 {db_path.stat().st_size/1e9:.1f} GB")

    print("[apply] 全部完成。chroma 物理空间回收请另跑 --prune-chroma-orphans(约 15G)。")
    print(f"[apply] 备份/进度目录：{backup_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="KB 过切清理(默认 dry-run，只读)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只读统计，零写入(默认)")
    mode.add_argument("--apply", action="store_true", help="正式执行(需 daemon 已停；默认先备份)")
    p.add_argument("--cap", type=int, default=_DEFAULT_CAP, help=f"单文档 chunk 上限(默认 {_DEFAULT_CAP})")
    p.add_argument("--db", default=str(DEFAULT_DB), help="kb_chunks.db 路径")
    p.add_argument("--chroma", default=str(DEFAULT_CHROMA), help="chroma/kb 目录")
    p.add_argument("--batch", type=int, default=500, help="chroma/sqlite 删除 id 批大小")
    p.add_argument("--backup-dir", default=None, help="备份+进度目录(默认 sqlite/ 下带时间戳)")
    p.add_argument("--progress", default=None, help="断点续跑进度文件(默认在 backup-dir 下)")
    p.add_argument("--skip-backup", action="store_true", help="跳过备份(高风险)")
    p.add_argument("--no-chroma", action="store_true", help="只处理 sqlite，不动 chroma 向量")
    p.add_argument("--no-vacuum", action="store_true", help="apply 后不 VACUUM")
    p.add_argument("--force", action="store_true", help="跳过 WAL 活跃检查(确认 daemon 已停时)")
    p.add_argument("--prune-chroma-orphans", action="store_true",
                   help="回收 chroma 孤儿 segment 目录(约 15G，与 trim 独立)。配合 --apply 才真删")
    p.add_argument("--show-top", type=int, default=20, help="dry-run 打印 TOP N 过切文档")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    chroma_path = Path(args.chroma)

    # chroma 孤儿回收(独立子流程)
    if args.prune_chroma_orphans:
        apply = bool(args.apply)
        bdir = Path(args.backup_dir) if args.backup_dir else (
            db_path.parent / f"kb_trim_backup_{time.strftime('%Y%m%d_%H%M%S')}"
        )
        r = prune_chroma_orphans(chroma_path, apply=apply, backup_dir=bdir if apply else None)
        tag = "APPLY" if apply else "DRY-RUN"
        print(f"[{tag}] chroma 孤儿回收：磁盘目录 {r['total_dirs']}，孤儿 {r['orphans']}，"
              f"可回收约 {r['reclaim_mb']} MB，已删 {r['deleted']} 个。")
        if not apply:
            print("  这是 DRY-RUN，未删任何目录。加 --apply 才真删(会先备份 chroma.sqlite3)。")
        return 0

    if not db_path.exists():
        print(f"[error] 找不到 DB: {db_path}")
        return 1

    if args.apply:
        return run_apply(args)

    # 默认 dry-run
    ro = open_ro(db_path)
    try:
        plan = plan_trim(ro, args.cap)
    finally:
        ro.close()
    print_plan(plan, db_path, chroma_path, show_top=args.show_top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# =============================================================================
# RUNBOOK — 正式执行步骤（由人工在确认后执行；本注释是唯一权威操作序）
# =============================================================================
# 前置：本机(darwin)，python=/Volumes/MacMini/opt/miniconda3/envs/antigravity/bin/python3.12
#       所有命令带 HF_HUB_OFFLINE=1（虽然 trim 不 embed，chroma_factory 初始化时也不联网）。
#
# 0) 预演（安全，随时可跑，不动数据）:
#    HF_HUB_OFFLINE=1 <py> APP/backend/scripts/kb_rechunk_trim.py --dry-run --cap 300
#
# 1) 暂停 KB 写入调度（否则会与脚本抢写锁）:
#    - 关闭 hourly / daily 全量与增量 KB 刷新：
#      kb-refresh-incremental (cron 0 * * * *)、kb-refresh-full (cron 0 2 * * *)、
#      weekly-kb-full-reindex、daily-kb-incremental-sync。
#      经 API: PATCH /api/schedules/{id} {"enabled": false}（admin），或直接停 jobmaster daemon。
#    - 停 jobmaster daemon（本机由 launchctl com.aiticket.local-* 托管，按现有运维手册停）。
#    - 确认后端/daemon 已不再写库：反复看 kb_chunks.db-wal 大小是否稳定不涨。
#
# 2) 备份（脚本 --apply 会自动做；也可外层再快照一次）:
#    - sqlite：脚本用 sqlite backup API 生成 kb_chunks.db.bak（一致、含 WAL）。
#    - chroma：脚本 copy chroma.sqlite3 + 记录段目录清单 chroma_kb_dirs_before.txt。
#    - 备份目录默认 sqlite/kb_trim_backup_<ts>/，务必放在有 >=10G 余量的卷（可用 --backup-dir）。
#
# 3) 正式 trim（截尾到 cap，逐文档提交、可断点续跑、结束 VACUUM）:
#    HF_HUB_OFFLINE=1 <py> APP/backend/scripts/kb_rechunk_trim.py --apply --cap 300 \
#        --backup-dir /Volumes/MacMini/kb_trim_backup
#    中断后重跑同命令即自动续跑（进度文件 trim_progress.json）。
#
# 4) 回收 chroma 孤儿 segment 目录（约 15G，独立大头；先 dry-run 看数目再 apply）:
#    HF_HUB_OFFLINE=1 <py> APP/backend/scripts/kb_rechunk_trim.py --prune-chroma-orphans           # 预演
#    HF_HUB_OFFLINE=1 <py> APP/backend/scripts/kb_rechunk_trim.py --prune-chroma-orphans --apply   # 真删
#    删后 chroma 元数据收缩：可选停机时对 chroma/kb/chroma.sqlite3 执行 VACUUM。
#
# 5) 验证:
#    - 只读复跑 --dry-run：受影响文档应为 0（幂等）。
#    - du -sh kb_chunks.db（应从 9G 降到约 2G 量级）、du -sh chroma/kb（应从 18G 降到 <4G）。
#    - 起后端做一次 KB 检索冒烟（board/search 命中若干过切手册仍能召回其前 cap 段）。
#
# 6) 恢复 daemon:
#    - 重新 enable 上述调度、启动 jobmaster daemon。
#    - 注意：全量 kb-refresh-full 会按 _MAX_CHUNKS_PER_DOC=300 重切，后续新入库文档自动受上限约束，
#      不会再复发过切（这是根治，而非一次性清理）。
#
# 回滚:
#    - sqlite：停 daemon → 用 kb_chunks.db.bak 覆盖 kb_chunks.db（删除 -wal/-shm 后再放）。
#    - chroma：若误删段目录 → 恢复 chroma.sqlite3.bak；段目录本身可由后端 repair_chroma() 从
#      sqlite chunks 重建（chroma 是派生索引，sqlite 才是真相源）。
# =============================================================================
