"""
/aiticket-kb <关键词>
搜索知识库。
用法：python scripts/search_kb.py "工作流超时" [--project EXAMPLE] [--top-k 5]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_client import AITicketClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if not args.query:
        print("用法: /aiticket-kb <查询词>")
        sys.exit(0)

    client = AITicketClient()
    project = args.project or client.default_project

    result = client.get(
        "/api/kb/search",
        query=args.query,
        project_key=project,
        top_k=args.top_k,
    )

    chunks = result.get("chunks", result.get("results", []))
    if not chunks:
        print("知识库中未找到相关内容")
        return

    print(f"KB 检索结果（top {len(chunks)}）：\n")
    for i, chunk in enumerate(chunks, 1):
        topic = chunk.get("topic", chunk.get("title", ""))
        content = chunk.get("content", chunk.get("text", ""))[:300]
        score = chunk.get("score", chunk.get("similarity", 0))
        score_pct = int(score * 100) if score <= 1 else int(score)
        print(f"[{i}] {topic}  ({score_pct}%)")
        print(f"    {content}...")
        print()


if __name__ == "__main__":
    main()
