"""
/aiticket-kb-upload — 上传文档到服务器知识库。
用法：python scripts/upload_kb.py <文件路径> [--title=标题] [--topic=话题] [--project=项目键] [--profile=PROFILE]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from api_client import AITicketClient


SUPPORTED_EXTS = {".md", ".txt", ".csv", ".html", ".xml", ".pdf", ".docx", ".pptx", ".xlsx"}


def main() -> None:
    parser = argparse.ArgumentParser(description="上传文档到 aiticket 知识库")
    parser.add_argument("filepath", help="要上传的文件路径")
    parser.add_argument("--title", default="", help="文档标题（默认：文件名）")
    parser.add_argument("--topic", default="", help="知识库 topic_l2 分类")
    parser.add_argument("--project", default="", help="项目键（默认：Profile 中的 default_project）")
    parser.add_argument("--profile", default=None, help="Profile 名称（默认：当前激活 Profile）")
    args = parser.parse_args()

    path = Path(args.filepath)
    if not path.exists():
        print(f"ERROR: File not found: {path}")
        sys.exit(1)

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        print(f"ERROR: Unsupported file type '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTS))}")
        sys.exit(1)

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > 20:
        print(f"ERROR: File too large ({size_mb:.1f} MB). Maximum 20 MB.")
        sys.exit(1)

    title = args.title or path.name
    client = AITicketClient(args.profile)
    project = args.project or client.default_project

    print(f"Uploading {path.name} ({size_mb:.2f} MB) → {client._base}/api/kb/upload")
    if project:
        print(f"  project_key: {project}")
    if args.topic:
        print(f"  topic_l2: {args.topic}")

    with open(path, "rb") as f:
        result = client.post_multipart(
            "/api/kb/upload",
            files={"file": (path.name, f, _mime(ext))},
            data={k: v for k, v in {
                "title": title,
                "project_key": project,
                "topic_l2": args.topic,
            }.items() if v},
        )

    print(f"\n✓ Uploaded successfully")
    print(f"  item_id    : {result.get('item_id', '?')}")
    print(f"  project_key: {result.get('project_key', '?')}")
    print(f"  chunks     : {result.get('chunks', '?')}")


def _mime(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".html": "text/html",
        ".xml": "application/xml",
    }.get(ext, "application/octet-stream")


if __name__ == "__main__":
    main()
