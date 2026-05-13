"""
/aiticket-reply <工单号>
为指定工单生成智能回复。
用法：python scripts/generate_reply.py EXAMPLE-1001
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api_client import AITicketClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_key", nargs="?", default="")
    args = parser.parse_args()

    if not args.issue_key:
        print("用法: /aiticket-reply <工单号>  例如: /aiticket-reply EXAMPLE-1001")
        sys.exit(0)

    client = AITicketClient()
    print(f"正在为 {args.issue_key} 生成智能回复...")

    result = client.post(
        "/api/board/generate-reply",
        {"issue_key": args.issue_key},
    )

    reply = result.get("reply", result.get("content", ""))
    if not reply:
        print("未能生成回复，请检查 issue key 是否正确")
        return

    print(f"\n{'─'*60}")
    print(f"Issue: {args.issue_key}")
    print(f"{'─'*60}")
    print(reply)
    print(f"{'─'*60}")
    print("\n（以上内容已加载到对话上下文，可继续要求 Claude 修改或直接复制使用）")


if __name__ == "__main__":
    main()
