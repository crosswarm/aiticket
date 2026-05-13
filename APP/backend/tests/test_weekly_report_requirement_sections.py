import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_weekly_report_includes_requirements_pool_and_process_labeled_sections(tmp_path, monkeypatch):
    import weekly_analysis

    src_dir = tmp_path / "src"
    report_dir = tmp_path / "WeeklyReports"
    src_dir.mkdir()
    report_dir.mkdir()

    crewlist = tmp_path / "crewlist.md"
    crewlist.write_text("- qiangxiao, 强晓\n- tester, 测试同学\n", encoding="utf-8")

    topic = tmp_path / "topic.md"
    topic.write_text("# topics\n", encoding="utf-8")

    csv_name = "工作流-周数据-2026-03-09-2026-03-15T19_58_40+0800.csv"
    csv_path = src_dir / csv_name
    pd.DataFrame(
        [
            {
                "问题关键字": "MYPROJECT-60001",
                "概要": "客户希望将流程监控问题纳入需求库",
                "项目名称": "云平台-流程中心",
                "经办人": "qiangxiao",
                "创建日期": "2026-03-10 09:00:00",
                "解决日期": "2026-03-11 11:00:00",
                "自定义字段(研发确认问题类型)": "需求问题",
                "自定义字段(解决方案)": "纳入需求库统一规划",
                "自定义字段(回复方式)": "纳入需求库",
                "标签": "流程-监控-查询",
            },
            {
                "问题关键字": "MYPROJECT-60002",
                "概要": "审批面板展示问题需要关注",
                "项目名称": "云平台-流程中心",
                "经办人": "tester",
                "创建日期": "2026-03-12 10:00:00",
                "解决日期": "2026-03-13 12:00:00",
                "自定义字段(研发确认问题类型)": "需求问题",
                "自定义字段(解决方案)": "先记录重点关注问题",
                "自定义字段(回复方式)": "待排期",
                "标签": "流程-审批面板",
            },
        ]
    ).to_csv(csv_path, index=False)

    monkeypatch.setattr(weekly_analysis, "SRC_DIR", str(src_dir))
    monkeypatch.setattr(weekly_analysis, "REPORT_DIR", str(report_dir))
    monkeypatch.setattr(weekly_analysis, "CREWLIST_PATH", str(crewlist))
    monkeypatch.setattr(weekly_analysis, "TOPIC_PATH", str(topic))
    monkeypatch.setattr(weekly_analysis.LLMService, "call_llm", lambda *args, **kwargs: "AI总结")

    analyzer = weekly_analysis.WeeklyAnalyzer()
    result = analyzer.run(
        api_key="fake-key",
        provider="openai",
        model_name="fake-model",
        base_url="http://fake",
        csv_filename=csv_name,
        force=True,
    )

    assert result["status"] == "success"

    json_path = report_dir / "Weekly_Report_2026-03-09_2026-03-15.json"
    md_path = report_dir / "Weekly_Report_2026-03-09_2026-03-15.md"
    assert json_path.exists()
    assert md_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")

    assert payload["meta"]["has_requirements_pool"] is True
    assert payload["meta"]["has_labeled_issues"] is True
    assert payload["requirements_pool"]["total_count"] == 1
    assert payload["requirements_pool"]["requirements"][0]["问题关键字"] == "MYPROJECT-60001"
    assert len(payload["labeled_issues"]["process_labeled"]) == 2

    assert "### 纳入需求库统计" in markdown
    assert "### 纳入需求库问题清单" in markdown
    assert "### \"流程-\"标签重点关注问题" in markdown
    assert "MYPROJECT-60001" in markdown
    assert "MYPROJECT-60002" in markdown
