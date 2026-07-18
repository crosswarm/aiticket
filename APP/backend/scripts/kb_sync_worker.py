"""
KB sync 独立子进程 worker。

背景（内存泄漏根因 B「断泵」）：
  长驻 jobmaster 进程内直接调 KnowledgeRuntimeService().sync() 会全新构造
  KnowledgeHybridIndex → chromadb.PersistentClient 载入 18GB / 63 万 chunk 索引段，
  进程长驻导致 chroma 内存跨次构造不释放，累积到数十 GB → swap 抖动 → 全机卡顿。
  本脚本作为独立进程执行一次 sync，退出即由 OS 完全回收内存（对齐 pattern_learning 的
  subprocess 模型）。

用法：
  python kb_sync_worker.py --mode incremental        # 增量 sync
  python kb_sync_worker.py --mode full               # 全量 force_refresh sync + compile_all
  python kb_sync_worker.py --mode incremental --dry-run  # 只验证 import 链，不触碰 18GB 库

输出契约：
  成功：以单行 JSON 打印结果到 stdout（内部 [KBService] print 噪音已重定向到 stderr），
        父进程解析 stdout 最后一行 JSON。
  失败：非 0 退出码 + traceback 打印到 stderr。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
from pathlib import Path

# 强制离线，禁止子进程联网下载模型（daemon 环境约定 HF_HUB_OFFLINE=1）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def _run_incremental() -> dict:
    """增量 sync：等价于原 _task_kb_refresh_incremental 的 KnowledgeRuntimeService().sync()。"""
    from kb_runtime_service import KnowledgeRuntimeService
    result = KnowledgeRuntimeService().sync()
    return {
        "mode": "incremental",
        "ok": True,
        "chunk_count": result.get("chunk_count"),
        "local_manifest_count": result.get("local_manifest_count"),
    }


def _run_full() -> dict:
    """全量 sync + compile：等价于原 _task_kb_refresh_full 的
    sync(force_refresh=True) 然后 get_or_create_compile_service().compile_all()。
    """
    from kb_runtime_service import KnowledgeRuntimeService
    sync_result = KnowledgeRuntimeService().sync(force_refresh=True)
    from kb_compile_service import get_or_create_compile_service
    compiled = get_or_create_compile_service().compile_all()
    return {
        "mode": "full",
        "ok": True,
        "chunk_count": sync_result.get("chunk_count"),
        "local_manifest_count": sync_result.get("local_manifest_count"),
        "compiled_topics": len(compiled),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KB sync 独立子进程 worker（防长驻进程 chroma 内存泄漏）"
    )
    parser.add_argument(
        "--mode", required=True, choices=["incremental", "full"],
        help="incremental=增量 sync；full=全量 force_refresh sync + compile_all",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只验证 import 链可用（不构造索引、不触碰 18GB 库），用于参数/启动自检",
    )
    args = parser.parse_args()

    # 内部 sync/compile 会大量 print([KBService] ...) 到 stdout，会污染结果解析。
    # 把这些噪音重定向到 stderr，保持 stdout 只承载最终一行 JSON。
    if args.dry_run:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                from kb_runtime_service import KnowledgeRuntimeService  # noqa: F401
                if args.mode == "full":
                    from kb_compile_service import get_or_create_compile_service  # noqa: F401
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            sys.stderr.write(f"\n[kb_sync_worker] dry-run import 失败: {e}\n")
            return 1
        print(json.dumps({"mode": args.mode, "ok": True, "dry_run": True}, ensure_ascii=False))
        return 0

    try:
        with contextlib.redirect_stdout(sys.stderr):
            if args.mode == "incremental":
                payload = _run_incremental()
            else:
                payload = _run_full()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        sys.stderr.write(f"\n[kb_sync_worker] {args.mode} sync 失败: {e}\n")
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
