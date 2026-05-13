"""
把 run_tests.py 输出的 JSON 渲染为可读 Markdown 报告。
用法: python build_report.py results-YYYYMMDD.json
"""
import json
import sys
from pathlib import Path


def render(data: dict) -> str:
    lines = [
        "# aiticket 压测验证报告",
        "",
        f"- **时间**: {data.get('timestamp', 'N/A')}",
        f"- **目标**: {data.get('base_url', 'N/A')}",
        f"- **整体结论**: {'✅ 全部通过' if data.get('passed_all') else '❌ 有测试未通过'}",
        "",
        "## 测试结果",
        "",
        "| 测试 | 请求数 | p50 (ms) | p95 (ms) | 错误率 | 结论 |",
        "|------|--------|---------|---------|--------|------|",
    ]

    for t in data.get("tests", []):
        passed = t.get("passed", False)
        icon = "✅" if passed else "❌"
        lines.append(
            f"| {t['test_id']} | {t['count']} | {t['p50_ms']} | {t['p95_ms']} | {t['error_rate']}% | {icon} |"
        )

    lines += ["", "## 详情"]
    for t in data.get("tests", []):
        lines.append(f"\n### {t['test_id']}")
        for k, v in t.items():
            if k not in ("test_id",):
                lines.append(f"- **{k}**: {v}")

    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "results.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    report = render(data)
    out_path = Path(path).with_suffix(".md")
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告已保存: {out_path}")
