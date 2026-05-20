#!/usr/bin/env python3
"""
KB 导入脚本 — 将 KB/ 目录下的文档向量化并存入知识库索引

支持格式：
  .md / .txt   直接索引
  .docx        自动提取文本
  .xlsx        自动提取表格文本
  .pdf         通过 markitdown 转换为 .md 后索引（需 markitdown：pip install markitdown）

用法：
  python scripts/import_kb.py              # 扫描并导入 KB/ 目录
  python scripts/import_kb.py --dir KB/hr  # 指定子目录
  python scripts/import_kb.py --convert-only  # 只转换 PDF，不触发后端同步
  python scripts/import_kb.py --reset      # 清空后重新索引（需要 backend 在运行）

依赖：
  运行中的 backend（http://localhost:18000）用于触发 /api/kb/sync
  可选：pip install markitdown（PDF 转换）
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_URL = "http://localhost:18000"


def convert_pdfs(kb_dir: Path) -> int:
    """将 kb_dir 下所有 .pdf 转换为 .md（如不存在同名 .md）"""
    pdfs = list(kb_dir.rglob("*.pdf"))
    if not pdfs:
        return 0

    try:
        import markitdown as _md  # noqa — check import
        from markitdown import MarkItDown
    except ImportError:
        print("[import_kb] ⚠️  markitdown 未安装，跳过 PDF 转换。")
        print("            安装：pip install markitdown")
        return 0

    converter = MarkItDown()
    converted = 0
    for pdf in pdfs:
        md_out = pdf.with_suffix(".md")
        if md_out.exists():
            print(f"[import_kb] 跳过（.md 已存在）: {pdf.relative_to(ROOT)}")
            continue
        try:
            result = converter.convert(str(pdf))
            md_out.write_text(result.text_content, encoding="utf-8")
            print(f"[import_kb] ✅ PDF → MD: {pdf.relative_to(ROOT)}")
            converted += 1
        except Exception as e:
            print(f"[import_kb] ❌ PDF 转换失败 {pdf.name}: {e}")

    return converted


def trigger_sync(reset: bool = False) -> bool:
    """通过 backend API 触发 KB 同步"""
    try:
        import requests
    except ImportError:
        print("[import_kb] ⚠️  requests 未安装，无法触发 backend sync。")
        print("            请手动调用：curl -X POST http://localhost:18000/api/kb/sync")
        return False

    try:
        # 检查 backend 是否在运行
        r = requests.get(f"{BACKEND_URL}/health", timeout=5)
        if r.status_code != 200:
            raise ConnectionError(f"backend 返回 {r.status_code}")
    except Exception as e:
        print(f"[import_kb] ❌ backend 未响应（{e}）")
        print("            请先启动 backend，然后再运行此脚本，或手动调用：")
        print(f"            curl -X POST {BACKEND_URL}/api/kb/sync")
        return False

    if reset:
        print("[import_kb] 执行 reset + sync...")
        # 没有单独的 reset API；sync 内部会清理并重建 kb_local 条目
    else:
        print("[import_kb] 触发 KB 增量同步...")

    r = requests.post(f"{BACKEND_URL}/api/kb/sync", timeout=120)
    if r.status_code == 200:
        data = r.json()
        local_count = data.get("local_manifest_count", "?")
        chunk_count = data.get("chunk_count", "?")
        print(f"[import_kb] ✅ 同步完成：{local_count} 篇文档，{chunk_count} 个 chunks")
        return True
    else:
        print(f"[import_kb] ❌ sync 失败: HTTP {r.status_code} — {r.text[:200]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="导入 KB 文档到知识库索引")
    parser.add_argument("--dir", default="KB", help="KB 文档目录（默认：KB/）")
    parser.add_argument("--convert-only", action="store_true",
                        help="只转换 PDF 为 .md，不触发 backend sync")
    parser.add_argument("--reset", action="store_true",
                        help="清空后重新索引（sync 内部重建 kb_local）")
    args = parser.parse_args()

    kb_dir = ROOT / args.dir
    if not kb_dir.exists():
        print(f"[import_kb] ❌ 目录不存在: {kb_dir}")
        sys.exit(1)

    print(f"[import_kb] KB 目录: {kb_dir.relative_to(ROOT)}")

    # 1. 转换 PDF → MD
    converted = convert_pdfs(kb_dir)
    if converted:
        print(f"[import_kb] 转换了 {converted} 个 PDF 文件")

    if args.convert_only:
        print("[import_kb] --convert-only 模式，跳过 backend sync")
        return

    # 2. 触发后端同步
    ok = trigger_sync(reset=args.reset)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
