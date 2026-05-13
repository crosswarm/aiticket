"""
报告行动化改造 - TDD 测试套件
对应设计: _local/design/plans/2026-03-15-report-actionability-improvement.md

测试目标:
  S1: call_llm 过滤 <think> 标签
  S2: 周报 TOP10 prompt 含四维行动标签
  S3: 月报 TOP10 prompt 含四维行动标签
  S4: 月报跨周持续性分析方法
  S5: 月报下月行动计划方法
  S6: 角色名归一化修复 YoY 失真
  S7: 月报结构包含新章节

运行方式:
  python3 -m pytest APP/backend/tests/test_report_quality.py -v
"""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────────────
# S1: <think> 标签过滤
# ─────────────────────────────────────────────────────────────────────────────

def test_call_llm_strips_think_tags(monkeypatch):
    """call_llm 必须过滤单个 <think>...</think> 块"""
    from llm_service import LLMService
    service = LLMService()
    raw = "<think>\n内部推理过程\n复杂思考步骤\n</think>\n## 分析结果\n正文内容"
    monkeypatch.setattr(service, "_call_openai_with_retry",
                        lambda *a, **k: iter([raw]))
    result = service.call_llm("test prompt", api_key="fake-key", provider="openai",
                               model_name="test-model", base_url="http://fake")
    assert "<think>" not in result
    assert "</think>" not in result
    assert "内部推理过程" not in result
    assert "复杂思考步骤" not in result
    assert "分析结果" in result
    assert "正文内容" in result


def test_call_llm_strips_multiple_think_blocks(monkeypatch):
    """call_llm 必须过滤多个 <think> 块"""
    from llm_service import LLMService
    service = LLMService()
    raw = "<think>第一段思考</think>中间内容<think>第二段思考</think>末尾内容"
    monkeypatch.setattr(service, "_call_openai_with_retry",
                        lambda *a, **k: iter([raw]))
    result = service.call_llm("test", api_key="fake", provider="openai",
                               model_name="m", base_url="http://x")
    assert "<think>" not in result
    assert "第一段思考" not in result
    assert "第二段思考" not in result
    assert "中间内容" in result
    assert "末尾内容" in result


def test_call_llm_preserves_normal_content(monkeypatch):
    """call_llm 对不含 <think> 的正常内容不应有任何修改"""
    from llm_service import LLMService
    service = LLMService()
    raw = "## TOP10聚类\n| 排名 | 问题 | 数量 |\n| 1 | 审批流问题 | 22 |"
    monkeypatch.setattr(service, "_call_openai_with_retry",
                        lambda *a, **k: iter([raw]))
    result = service.call_llm("test", api_key="fake", provider="openai",
                               model_name="m", base_url="http://x")
    assert "TOP10聚类" in result
    assert "审批流问题" in result
    assert "22" in result


# ─────────────────────────────────────────────────────────────────────────────
# S2: 周报 TOP10 Prompt 包含四维行动标签
# ─────────────────────────────────────────────────────────────────────────────

def test_weekly_focus_prompt_contains_all_action_labels():
    """weekly_analysis.py 的 focus_prompt 必须包含四维行动标签定义"""
    import weekly_analysis
    src = inspect.getsource(weekly_analysis.WeeklyAnalyzer.run)
    assert "A-用户培训" in src, "缺少 A-用户培训 标签"
    assert "B-顾问赋能" in src, "缺少 B-顾问赋能 标签"
    assert "C-诊断工具" in src, "缺少 C-诊断工具 标签"
    assert "D-产品规划" in src, "缺少 D-产品规划 标签"


def test_weekly_focus_prompt_requires_action_table_columns():
    """周报 prompt 必须要求输出行动类型列和责任方列"""
    import weekly_analysis
    src = inspect.getsource(weekly_analysis.WeeklyAnalyzer.run)
    assert "行动类型" in src, "缺少 行动类型 列要求"
    assert "责任方" in src, "缺少 责任方 列要求"
    assert "预计降量" in src, "缺少 预计降量 列要求"


def test_weekly_focus_prompt_requires_action_summary():
    """周报 prompt 必须要求 LLM 输出本周可执行行动清单"""
    import weekly_analysis
    src = inspect.getsource(weekly_analysis.WeeklyAnalyzer.run)
    assert "本周可执行行动清单" in src, "缺少行动清单要求"


# ─────────────────────────────────────────────────────────────────────────────
# S3: 月报 TOP10 Prompt 包含四维行动标签
# ─────────────────────────────────────────────────────────────────────────────

def test_monthly_top10_csv_prompt_contains_action_labels():
    """_generate_top10_focus (CSV模式) 的 prompt 必须包含四维行动标签"""
    from monthly_analysis import MonthlyReportGenerator
    src = inspect.getsource(MonthlyReportGenerator._generate_top10_focus)
    assert "A-用户培训" in src
    assert "B-顾问赋能" in src
    assert "C-诊断工具" in src
    assert "D-产品规划" in src
    assert "月度行动摘要" in src


def test_monthly_top10_tickets_prompt_contains_action_labels():
    """_generate_top10_focus_from_tickets (周报聚合模式) 的 prompt 必须包含四维行动标签"""
    from monthly_analysis import MonthlyReportGenerator
    src = inspect.getsource(MonthlyReportGenerator._generate_top10_focus_from_tickets)
    assert "A-用户培训" in src
    assert "B-顾问赋能" in src
    assert "C-诊断工具" in src
    assert "D-产品规划" in src


# ─────────────────────────────────────────────────────────────────────────────
# S6: 角色名归一化
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_role_maps_english_to_chinese():
    """_normalize_role 必须将英文角色名正确映射到中文"""
    from monthly_analysis import YoYAnalyzer
    assert YoYAnalyzer._normalize_role("Developer") == "开发"
    assert YoYAnalyzer._normalize_role("developer") == "开发"
    assert YoYAnalyzer._normalize_role("Dev") == "开发"
    assert YoYAnalyzer._normalize_role("Product Manager") == "产品经理"
    assert YoYAnalyzer._normalize_role("product manager") == "产品经理"
    assert YoYAnalyzer._normalize_role("Unknown") == "其他"
    assert YoYAnalyzer._normalize_role("未知") == "其他"


def test_normalize_role_preserves_chinese():
    """_normalize_role 对已是中文的角色名应保持不变"""
    from monthly_analysis import YoYAnalyzer
    assert YoYAnalyzer._normalize_role("开发") == "开发"
    assert YoYAnalyzer._normalize_role("产品经理") == "产品经理"
    assert YoYAnalyzer._normalize_role("测试") == "测试"


def test_yoy_metrics_merges_normalized_roles():
    """同比角色对比必须合并 Developer 和 开发 为同一行，消除 ↓100% 失真"""
    from monthly_analysis import YoYAnalyzer
    analyzer = YoYAnalyzer()
    current = {
        'meta': {'total_tickets': 100, 'count_process': 90,
                 'count_transferred': 10, 'ratio_transferred': 10.0},
        'charts': {
            'type_counts': {},
            'role_counts': {'Developer': 60.0, '产品经理': 40.0}  # 今年用英文
        }
    }
    last_year = {
        'meta': {'total_tickets': 120, 'count_process': 100,
                 'count_transferred': 20, 'ratio_transferred': 16.7},
        'charts': {
            'type_counts': {},
            'role_counts': {'开发': 65.0, 'Product Manager': 35.0}  # 去年用中文
        }
    }
    metrics = analyzer.calculate_yoy_metrics(current, last_year)
    roles = {r['role']: r for r in metrics['role_comparison']}

    # 归一化后应合并，不能同时存在 Developer 和 开发
    assert '开发' in roles, "归一化后应有 '开发' 角色"
    assert 'Developer' not in roles, "Developer 应被归一化为 '开发'"
    assert '产品经理' in roles, "归一化后应有 '产品经理' 角色"
    assert 'Product Manager' not in roles, "Product Manager 应被归一化为 '产品经理'"

    # 同比变化应有意义（不能是 None，说明两年数据都对上了）
    assert roles['开发']['change'] is not None, "开发角色同比变化不应为 None（说明归一化生效）"


# ─────────────────────────────────────────────────────────────────────────────
# S4: 跨周持续性分析方法
# ─────────────────────────────────────────────────────────────────────────────

def test_cross_week_analysis_method_exists():
    """MonthlyReportGenerator 必须有 _analyze_cross_week_patterns 方法"""
    from monthly_analysis import MonthlyReportGenerator
    assert hasattr(MonthlyReportGenerator, '_analyze_cross_week_patterns')
    assert callable(getattr(MonthlyReportGenerator, '_analyze_cross_week_patterns'))


def test_cross_week_analysis_returns_empty_for_single_week(monkeypatch):
    """少于 2 周数据时，跨周分析返回空字符串（不崩溃）"""
    import monthly_analysis as ma
    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    generator.monthly_analyzer = ma.MonthlyAnalyzer()

    monkeypatch.setattr(
        generator.monthly_analyzer, "_get_weekly_reports_for_month",
        lambda y, m: [{'content': 'week1', '_date': '20260301'}]
    )
    result = generator._analyze_cross_week_patterns(
        2026, 3, "fake-key", "openai", "test", "http://fake"
    )
    assert result == ""


def test_cross_week_analysis_returns_empty_for_no_weeks(monkeypatch):
    """没有周报数据时，跨周分析返回空字符串"""
    import monthly_analysis as ma
    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    generator.monthly_analyzer = ma.MonthlyAnalyzer()

    monkeypatch.setattr(
        generator.monthly_analyzer, "_get_weekly_reports_for_month",
        lambda y, m: []
    )
    result = generator._analyze_cross_week_patterns(
        2026, 3, "fake-key", "openai", "test", "http://fake"
    )
    assert result == ""


def test_cross_week_analysis_calls_llm_with_multiple_weeks(monkeypatch):
    """有 2 周以上数据时，跨周分析应调用 LLM 并返回分析结果"""
    import monthly_analysis as ma
    from llm_service import LLMService

    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    generator.monthly_analyzer = ma.MonthlyAnalyzer()

    weekly_data = [
        {'content': '## 4. 工单重点关注 (TOP 10 聚类)\n审批流配置问题（22条）', '_date': '20260301'},
        {'content': '## 4. 工单重点关注 (TOP 10 聚类)\n审批流配置问题（18条）', '_date': '20260308'},
    ]
    monkeypatch.setattr(
        generator.monthly_analyzer, "_get_weekly_reports_for_month",
        lambda y, m: weekly_data
    )

    llm_received_prompt = {}

    def fake_call_llm(self_or_prompt, prompt_or_nothing=None, **kwargs):
        # 兼容实例方法和模块级调用
        p = prompt_or_nothing if prompt_or_nothing else self_or_prompt
        llm_received_prompt['text'] = p
        return "### 跨周持续问题\n| 审批流配置 | 2周 | 未改善 | 高 | 立即处理 |"

    monkeypatch.setattr(LLMService, "call_llm", fake_call_llm)

    result = generator._analyze_cross_week_patterns(
        2026, 3, "fake-key", "openai", "test", "http://fake"
    )
    assert "审批流" in result or len(result) > 0  # LLM 被调用并返回内容


# ─────────────────────────────────────────────────────────────────────────────
# S5: 下月行动计划方法
# ─────────────────────────────────────────────────────────────────────────────

def test_next_month_plan_method_exists():
    """MonthlyReportGenerator 必须有 _generate_next_month_action_plan 方法"""
    from monthly_analysis import MonthlyReportGenerator
    assert hasattr(MonthlyReportGenerator, '_generate_next_month_action_plan')
    assert callable(getattr(MonthlyReportGenerator, '_generate_next_month_action_plan'))


def test_next_month_plan_returns_empty_without_api_key():
    """没有 api_key 时下月计划返回空字符串"""
    import monthly_analysis as ma
    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)

    result = generator._generate_next_month_action_plan(
        2026, 3, "some top10 analysis", "some cross week",
        api_key="", provider="openai", model_name="test", base_url="http://fake"
    )
    assert result == ""


def test_next_month_plan_prompt_includes_next_month(monkeypatch):
    """下月计划 prompt 应包含正确的下月月份"""
    import monthly_analysis as ma
    from llm_service import LLMService

    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    captured = {}

    def fake_call_llm(self_or_prompt, prompt_or_nothing=None, **kwargs):
        p = prompt_or_nothing if prompt_or_nothing else self_or_prompt
        captured['prompt'] = p
        return "## 2026年4月行动计划\nA类: 培训"

    monkeypatch.setattr(LLMService, "call_llm", fake_call_llm)

    generator._generate_next_month_action_plan(
        2026, 3, "top10", "crossweek",
        api_key="fake", provider="openai", model_name="m", base_url="http://x"
    )
    # 3月的下月是4月
    assert "4月" in captured.get('prompt', '') or "4" in captured.get('prompt', '')


def test_next_month_plan_december_rolls_over_to_january(monkeypatch):
    """12月的下月行动计划应指向次年1月"""
    import monthly_analysis as ma
    from llm_service import LLMService

    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    captured = {}

    def fake_call_llm(self_or_prompt, prompt_or_nothing=None, **kwargs):
        p = prompt_or_nothing if prompt_or_nothing else self_or_prompt
        captured['prompt'] = p
        return "## 2027年1月行动计划"

    monkeypatch.setattr(LLMService, "call_llm", fake_call_llm)

    result = generator._generate_next_month_action_plan(
        2026, 12, "top10", "crossweek",
        api_key="fake", provider="openai", model_name="m", base_url="http://x"
    )
    # 应提到 2027年1月（或 1月）
    assert "1月" in captured.get('prompt', '') or "2027" in captured.get('prompt', '')


# ─────────────────────────────────────────────────────────────────────────────
# S7: 月报结构包含新章节
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_monthly_data():
    """构造最小化 monthly_data 用于测试"""
    return {
        'meta': {
            'total_tickets': 5, 'count_process': 5,
            'count_transferred': 0, 'ratio_transferred': 0.0,
            'source_type': 'test'
        },
        'charts': {
            'type_counts': {}, 'role_counts': {}, 'assignee_counts': {},
            'daily_counts': {}, 'req_counts': {}, 'trans_counts': {}, 'op_counts': {}
        },
        'ticket_details': {
            'requirement_tickets': [], 'operation_tickets': [],
            'transferred_tickets': [], 'implementation_tickets': [],
            'ops_tickets': [], 'all_tickets': []
        }
    }


def test_report_content_includes_cross_week_section_when_present():
    """月报 _generate_report_content 在有跨周分析时，内容中应包含第10节"""
    import monthly_analysis as ma
    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    generator.monthly_analyzer = ma.MonthlyAnalyzer()
    generator.yoy_analyzer = ma.YoYAnalyzer()

    content = generator._generate_report_content(
        monthly_data=_minimal_monthly_data(),
        yoy_metrics={}, mom_metrics={},
        last_year_data=None, last_month_data=None,
        year=2026, month=3,
        top10_analysis="", detailed_tables="",
        cross_week_analysis="### 跨周持续问题\n| 审批流 | 2周 |",
        next_month_plan="",
    )
    assert "跨周持续性分析" in content, "月报内容应包含跨周持续性分析章节标题"
    assert "跨周持续问题" in content, "月报内容应包含跨周分析的实际内容"


def test_report_content_includes_next_month_plan_when_present():
    """月报 _generate_report_content 在有行动计划时，内容中应包含行动计划"""
    import monthly_analysis as ma
    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    generator.monthly_analyzer = ma.MonthlyAnalyzer()
    generator.yoy_analyzer = ma.YoYAnalyzer()

    content = generator._generate_report_content(
        monthly_data=_minimal_monthly_data(),
        yoy_metrics={}, mom_metrics={},
        last_year_data=None, last_month_data=None,
        year=2026, month=3,
        top10_analysis="", detailed_tables="",
        cross_week_analysis="",
        next_month_plan="## 2026年4月行动计划\n### A类: 培训",
    )
    assert "行动计划" in content, "月报内容应包含行动计划"
    assert "A类" in content, "月报内容应包含行动计划的具体内容"


def test_report_content_omits_empty_sections():
    """月报 _generate_report_content 在无跨周/行动数据时不应输出空章节标题"""
    import monthly_analysis as ma
    generator = ma.MonthlyReportGenerator.__new__(ma.MonthlyReportGenerator)
    generator.monthly_analyzer = ma.MonthlyAnalyzer()
    generator.yoy_analyzer = ma.YoYAnalyzer()

    content = generator._generate_report_content(
        monthly_data=_minimal_monthly_data(),
        yoy_metrics={}, mom_metrics={},
        last_year_data=None, last_month_data=None,
        year=2026, month=3,
        top10_analysis="", detailed_tables="",
        cross_week_analysis="",
        next_month_plan="",
    )
    assert "跨周持续性分析" not in content, "空跨周分析时不应输出章节标题"
    assert "下月行动计划" not in content, "空行动计划时不应输出章节标题"
