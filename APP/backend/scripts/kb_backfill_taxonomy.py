#!/usr/bin/env python3
"""把 KB 文档的 l1_module / l2_module 回填为 BIP 主数据分类（label / application）。

改造前 ``l1_module`` 是 KB 目录名（"业务流"、"bip-workflow"、"流程中心"…），
它是历史形成的、与产品主数据对不齐，也无法支撑跨领域知识库上线后的区隔。
改造后 ``l1_module`` = label 名、``l2_module`` = application 名。

**默认只分析不写库**，加 ``--apply`` 才真正 UPDATE。写库前会自动备份 DB。

用法::

    # 看归属报告（安全，什么都不改）
    python3 kb_backfill_taxonomy.py --kb-root /path/to/KB

    # 确认无误后写库
    python3 kb_backfill_taxonomy.py --kb-root /path/to/KB --db /path/to/kb_chunks.db --apply

172 上运行时 KB 与 DB 都在容器内::

    docker exec -w /app/APP/backend aiticket python3 scripts/kb_backfill_taxonomy.py \\
        --kb-root /app/KB --db /app/APP/data/sqlite/kb_chunks.db --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bip_taxonomy import BipTaxonomy  # noqa: E402

# 只读文档开头做证据提取：service_code 基本都出现在导航路径/菜单说明里，
# 全文读会把 15000 条编码的匹配成本放大到没必要的程度。
_TEXT_HEAD_CHARS = 8000


# KB 根下这些不是知识文档，是工程文件，被扫描器一并收了进来
_JUNK_SUFFIXES = (".py", ".json", ".pyc", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".lock")
_JUNK_NAMES = {"__init__", "index", "__pycache__"}


def derive_categories(source_rel_path: str, source_kind: str, name: str) -> tuple[str, str, bool]:
    """从索引里的 source_rel_path 推导 (top_category, second_category, 是否工程垃圾)。

    三种路径形态::

        KB/<top>/<second>/....docx    kb_local，正常知识文档
        compiled/<topic>              kb_compiled，主题编译产物，topic 即分类线索
        KB/__init__.py                工程文件，不是知识
    """
    rel = Path(source_rel_path or "")
    parts = rel.parts

    if source_kind == "kb_compiled" or (parts and parts[0] == "compiled"):
        topic = parts[1] if len(parts) > 1 else (name or "")
        return topic, "", False

    if parts and parts[0] == "KB":
        parts = parts[1:]

    if not parts:
        return "", "", True
    # KB 根下的裸文件（__init__.py / index.json）没有分类层级，直接判为垃圾
    if len(parts) == 1:
        stem = Path(parts[0]).stem
        is_junk = rel.suffix.lower() in _JUNK_SUFFIXES or stem in _JUNK_NAMES
        return "", "", is_junk

    top = parts[0]
    second = parts[1] if len(parts) > 2 else ""
    return top, second, False


def read_converted_text(kb_root: Path | None, source_rel_path: str) -> str:
    """取正文用于 service_code 证据。转换产物统一是 .md，源文件可能是 .docx/.xlsx。"""
    if not kb_root or not source_rel_path:
        return ""
    rel = Path(source_rel_path)
    if rel.parts and rel.parts[0] == "KB":
        rel = Path(*rel.parts[1:])
    candidate = kb_root / "OUTPUT" / "converted" / rel.with_suffix(".md")
    try:
        return candidate.read_text(encoding="utf-8", errors="ignore")[:_TEXT_HEAD_CHARS]
    except OSError:
        return ""


def analyse(db_path: Path, taxonomy: BipTaxonomy, kb_root: Path | None) -> list[dict]:
    """以索引库为准枚举文档——文件系统里的 converted 只是子集，且扩展名已变。"""
    if not db_path.is_file():
        sys.exit(f"DB 不存在：{db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        records = conn.execute(
            "SELECT content_id, source_rel_path, source_kind, name, l1_module, l2_module FROM documents"
        ).fetchall()
    finally:
        conn.close()

    rows = []
    for content_id, source_rel_path, source_kind, name, old_l1, old_l2 in records:
        top, second, is_junk = derive_categories(source_rel_path or "", source_kind or "", name or "")
        if is_junk:
            result_fields = {
                "label_code": "_excluded",
                "label_name": "_excluded",
                "application_code": "",
                "application_name": "",
                "domain_cloud_name": "",
                "evidence": "junk",
                "service_codes": [],
            }
        else:
            text = read_converted_text(kb_root, source_rel_path or "")
            # 编译主题名是工单聚类出来的自由文本，不是目录名，跨领域云匹配风险高
            strict = (source_kind or "") == "kb_compiled"
            result = taxonomy.classify(
                top_category=top, second_category=second, text=text, strict=strict
            )
            result_fields = {
                "label_code": result.label_code,
                "label_name": result.label_name,
                "application_code": result.application_code,
                "application_name": result.application_name,
                "domain_cloud_name": result.domain_cloud_name,
                "evidence": result.evidence,
                "service_codes": result.service_codes,
            }
        rows.append(
            {
                "content_id": content_id,
                "rel_path": source_rel_path or "",
                "source_kind": source_kind or "",
                "top_category": top,
                "second_category": second,
                "old_l1_module": old_l1 or "",
                "old_l2_module": old_l2 or "",
                **result_fields,
            }
        )
    return rows


def report(rows: list[dict]) -> None:
    total = len(rows)
    print(f"\n{'='*72}\n共 {total} 篇文档\n{'='*72}")

    by_evidence = Counter(r["evidence"] for r in rows)
    print("\n【归属依据分布】")
    for ev, n in by_evidence.most_common():
        mark = "⚠️ " if ev in ("fallback", "unavailable") else "   "
        print(f"  {mark}{ev:<14} {n:>4}  ({n/total*100:.1f}%)")

    # 成功率只看"应该归属"的文档：竞品资料和工程垃圾本就不该有 BIP 归属
    scope = [r for r in rows if r["label_code"] not in ("_external", "_excluded")]
    resolved = [r for r in scope if r["evidence"] not in ("fallback", "unavailable")]
    if scope:
        print(f"\n  归属成功率: {len(resolved)}/{len(scope)} = {len(resolved)/len(scope)*100:.1f}%"
              f"   （已排除 {total-len(scope)} 篇竞品/工程文件）")

    by_kind = Counter(r["source_kind"] or "(空)" for r in rows)
    print("\n【按来源】" + "  ".join(f"{k}={n}" for k, n in by_kind.most_common()))

    in_bip = [r for r in rows if r["label_code"] and r["label_code"] not in ("_external", "_excluded")]
    external = [r for r in rows if r["label_code"] == "_external"]
    excluded = [r for r in rows if r["label_code"] == "_excluded"]

    print(f"\n【按 label 聚合】BIP 体系内 {len(in_bip)} 篇")
    by_label = Counter(r["label_name"] or "(未归属)" for r in in_bip)
    for name, n in by_label.most_common():
        dc = next((r["domain_cloud_name"] for r in in_bip if (r["label_name"] or "(未归属)") == name), "")
        share = n / len(in_bip) * 100 if in_bip else 0
        print(f"  {name:<16} {n:>4}  ({share:4.1f}%)  {dc}")

    if external:
        print(f"\n【体系外 {len(external)} 篇】竞品资料，是知识但不属 BIP")
        for top, n in Counter(r["top_category"] for r in external).most_common():
            print(f"  {top:<18} {n:>4}")

    if excluded:
        print(f"\n【应剔除 {len(excluded)} 篇】非业务知识，被 KB 扫描器误收，正在污染召回")
        for r in excluded[:15]:
            print(f"  {r['rel_path']}")
        if len(excluded) > 15:
            print(f"  …还有 {len(excluded)-15} 篇")

    print("\n【目录 → 归属 映射结果】")
    by_dir: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        target = f"{r['label_name'] or '(未归属)'} / {r['application_name'] or '-'}"
        by_dir[r["top_category"]][target] += 1
    for top in sorted(by_dir):
        entries = by_dir[top].most_common()
        head = entries[0]
        extra = f"   +{len(entries)-1} 种其它归属" if len(entries) > 1 else ""
        print(f"  {top:<18} → {head[0]:<32} ×{head[1]}{extra}")
        for target, n in entries[1:]:
            print(f"  {'':<18}   {target:<32} ×{n}")

    unresolved = [r for r in rows if r["evidence"] in ("fallback", "unavailable")]
    if unresolved:
        print(f"\n【未归属 {len(unresolved)} 篇 —— 需人工补 overrides】")
        for top, n in Counter(r["top_category"] for r in unresolved).most_common():
            print(f"  {top:<18} {n:>4}")


def apply_to_db(db_path: Path, rows: list[dict]) -> None:
    """按 content_id 写回 documents 与 chunks（content_id 直接来自本次读取，不需要再匹配）。"""
    backup = db_path.with_suffix(db_path.suffix + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"\n已备份 DB → {backup}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        changed = updated_docs = updated_chunks = 0
        for r in rows:
            # 归属不出来的保持原值，不要把已有的目录名冲成空
            l1 = r["label_name"] or r["old_l1_module"] or r["top_category"]
            l2 = r["application_name"] or r["old_l2_module"] or r["second_category"]
            if l1 == r["old_l1_module"] and l2 == r["old_l2_module"]:
                continue
            changed += 1
            cur = conn.execute(
                "UPDATE documents SET l1_module=?, l2_module=? WHERE content_id=?",
                (l1, l2, r["content_id"]),
            )
            updated_docs += cur.rowcount
            cur = conn.execute(
                "UPDATE chunks SET l1_module=?, l2_module=? WHERE content_id=?",
                (l1, l2, r["content_id"]),
            )
            updated_chunks += cur.rowcount
        conn.commit()
    finally:
        conn.close()

    print(f"需变更 {changed}/{len(rows)} 篇；documents 更新 {updated_docs} 行，chunks 更新 {updated_chunks} 行")


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 KB 文档的 BIP 分类归属")
    parser.add_argument("--db", type=Path, required=True, help="kb_chunks.db 路径")
    parser.add_argument("--kb-root", type=Path, help="KB 根目录，给了才能用正文 service_code 证据")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只分析）")
    parser.add_argument("--report", type=Path, help="把逐篇明细写成 JSON")
    parser.add_argument("--taxonomy", type=Path, help="分类快照路径（默认 data/bip_taxonomy.json）")
    args = parser.parse_args()

    taxonomy = BipTaxonomy(path=args.taxonomy)
    if not taxonomy.available:
        print("⚠️  分类快照不可用，归属会全部退回目录名。先跑 export_bip_taxonomy.py。")

    rows = analyse(args.db, taxonomy, args.kb_root)
    report(rows)

    if args.report:
        args.report.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已写入 {args.report}")

    if args.apply:
        apply_to_db(args.db, rows)
    else:
        print("\n（未加 --apply，本次没有改动任何数据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
