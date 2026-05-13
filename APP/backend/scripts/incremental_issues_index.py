#!/usr/bin/env python3
"""
增量工单向量索引脚本 (MS-4-b)

从 Jira 缓存服务拉取最近更新的工单，将尚未在 Chroma issues_collection 里的
条目补充写入，避免新工单（如 MYPROJECT-62893）永远匹配不到的问题。

用法:
  python3 APP/backend/scripts/incremental_issues_index.py [--days N]
"""
import argparse
import os
import sys

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(BACKEND))

from vector_store import VectorStore
from jira_cache_service import get_jira_cache_service


def run(days: int = 2):
    print(f"[IncrementalIndex] 拉取最近 {days} 天更新的工单...")

    vs = VectorStore()
    svc = get_jira_cache_service()

    jql = f"project=MYPROJECT AND updated >= -{days}d ORDER BY updated DESC"
    try:
        result = svc.search_issues(jql, start_at=0, max_results=200)
    except Exception as e:
        print(f"[IncrementalIndex] Jira 查询失败: {e}")
        return 0

    issues = result.get("issues", []) if isinstance(result, dict) else []
    if not issues:
        print("[IncrementalIndex] 无新工单，跳过")
        return 0

    print(f"[IncrementalIndex] Jira 返回 {len(issues)} 条工单，检查哪些未入库...")

    to_add = []
    for issue in issues:
        key = issue.get("key", "")
        if not key:
            continue
        existing = vs.get_issue_by_key(key)
        if existing:
            continue
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        description = (fields.get("description") or summary)[:500]
        to_add.append({
            "key": key,
            "summary": summary,
            "description": description,
            "source": "jira_incremental",
        })

    if not to_add:
        print("[IncrementalIndex] 所有工单均已入库，无需更新")
        return 0

    print(f"[IncrementalIndex] 补充写入 {len(to_add)} 条: {[r['key'] for r in to_add[:10]]}")
    vs.batch_add_issues(to_add)
    print(f"[IncrementalIndex] 完成")
    return len(to_add)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2, help="拉取最近 N 天的工单（默认 2）")
    args = parser.parse_args()
    n = run(days=args.days)
    print(f"[IncrementalIndex] 新增 {n} 条")
