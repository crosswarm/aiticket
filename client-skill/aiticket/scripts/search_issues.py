"""
/aiticket-search <关键词>
通过关键词或 JQL 搜索看板工单。
用法：python scripts/search_issues.py "审批流退回" [--project EXAMPLE] [--limit 10]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_client import AITicketClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if not args.query:
        print("用法: /aiticket-search <关键词或JQL>")
        sys.exit(0)

    client = AITicketClient()
    project = args.project or client.default_project
    result = client.get(
        "/api/board/search",
        q=args.query,
        project_key=project,
        limit=args.limit,
    )

    issues = result.get("issues", result.get("results", []))
    if not issues:
        print("未找到匹配的 issue")
        return

    print(f"找到 {len(issues)} 条 issue：\n")
    for issue in issues:
        key = issue.get("key", "")
        title = issue.get("summary", issue.get("title", ""))
        status = issue.get("status", "")
        print(f"  [{key}] {title}  ({status})")


if __name__ == "__main__":
    main()
