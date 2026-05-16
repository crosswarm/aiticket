import hashlib
import os
import pandas as pd
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from collections import Counter
from analysis import CrewListParser, TopicParser, PROJECT_DISPLAY_NAMES
from llm_service import LLMService
def get_process_labeled_issues(df, label_pattern: str = "流程-") -> list:
    label_col = "标签"
    if label_col not in df.columns:
        return []
    mask = df[label_col].astype(str).str.contains(label_pattern, na=False, regex=False)
    return df[mask].to_dict("records")
from kpi_calculator import KPICalculator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
CREWLIST_PATH = os.path.join(PROJECT_ROOT, "_local/notes/crewlist.md")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
CONCLUSION_DIR = os.path.join(PROJECT_ROOT, "conclusion")
TOPIC_PATH = os.path.join(BASE_DIR, "data", "topic.md")
REPORT_DIR = os.path.join(CONCLUSION_DIR, "WeeklyReports")
os.makedirs(REPORT_DIR, exist_ok=True)

def _modules_slug(domain_modules: Optional[List[str]]) -> str:
    if not domain_modules:
        return ""
    return "_" + hashlib.md5(",".join(sorted(domain_modules)).encode()).hexdigest()[:6]


class WeeklyAnalyzer:
    def __init__(self, project_key: str = "MYPROJECT", domain_modules: Optional[List[str]] = None):
        self.project_key = project_key
        self.domain_modules = domain_modules or []
        self._modules_slug = _modules_slug(domain_modules)
        self.project_name = PROJECT_DISPLAY_NAMES.get(project_key, project_key)
        # MYPROJECT 写根目录保持历史兼容；其它项目写子目录
        if project_key == "MYPROJECT":
            self.report_dir = REPORT_DIR
        else:
            self.report_dir = os.path.join(REPORT_DIR, project_key)
        os.makedirs(self.report_dir, exist_ok=True)
        self.crew_parser = CrewListParser(CREWLIST_PATH)
        self.topic_parser = TopicParser(TOPIC_PATH, project_key)
        self.llm_service = LLMService()
        self.full_crew_map = self._parse_crew_realnames()

    def _parse_crew_realnames(self) -> Dict[str, str]:
        """
        Parse crewlist to get username -> realname mapping.
        Re-reads crewlist manually to get real names as CrewListParser only gives roles.
        """
        mapping = {}
        if not os.path.exists(CREWLIST_PATH):
            return mapping
            
        with open(CREWLIST_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith("-"):
                    # format: - username, name
                    clean = line.lstrip("- ").strip()
                    parts = re.split(r'[,，]', clean)
                    if len(parts) >= 2:
                        username = parts[0].strip()
                        realname = parts[1].strip()
                        mapping[username] = realname
        return mapping

    def get_realname(self, username: str) -> str:
        return self.full_crew_map.get(username, username)

    def load_latest_data(self) -> Tuple[pd.DataFrame, str]:
        if not os.path.exists(SRC_DIR):
            print(f"[WeeklyAnalysis] Source directory not found: {SRC_DIR}")
            return None, ""

        prefix = f"{self.project_key}{self._modules_slug}-"
        files = [f for f in os.listdir(SRC_DIR) if "周数据" in f and f.endswith(".csv") and f.startswith(prefix)]
        if not files:
            print(f"[WeeklyAnalysis] No weekly data files found in {SRC_DIR}")
            print(f"[WeeklyAnalysis] Available files: {os.listdir(SRC_DIR) if os.path.exists(SRC_DIR) else 'N/A'}")
            return None, ""

        # Sort by filename (which usually contains date)
        files.sort(reverse=True)
        latest_file = files[0]
        filepath = os.path.join(SRC_DIR, latest_file)
        print(f"Loading data from: {latest_file}")
        
        try:
            df = pd.read_csv(filepath)
            return df, latest_file
        except Exception as e:
            print(f"Error loading CSV: {e}")
            return None, ""

    def load_previous_week_report(self) -> Dict:
        """Load previous week's report JSON for comparison"""
        import json
        if not os.path.exists(self.report_dir):
            return {}
        
        json_files = [f for f in os.listdir(self.report_dir) if f.endswith('.json')]
        if len(json_files) < 2:
            return {}
        
        # Sort by filename to get second latest
        json_files.sort(reverse=True)
        prev_file = json_files[1] if len(json_files) > 1 else None
        
        if not prev_file:
            return {}
        
        try:
            with open(os.path.join(self.report_dir, prev_file), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading previous report: {e}")
            return {}

    def _format_compare(self, current: float, previous: float, unit: str = "%") -> str:
        """Format value with week-over-week comparison"""
        if previous == 0 or previous is None:
            return f"{current:.1f}{unit}"
        
        change = ((current - previous) / previous) * 100
        if change > 0:
            return f"{current:.1f}{unit} (↑{change:.1f}%环比)"
        elif change < 0:
            return f"{current:.1f}{unit} (↓{abs(change):.1f}%环比)"
        else:
            return f"{current:.1f}{unit} (持平)"

    def run(self, api_key: str = None, provider: str = "gemini", model_name: str = "", base_url: str = "",
            csv_filename: str = None, force: bool = False):
        if not api_key:
             # Try env var as fallback
             api_key = os.environ.get("LLM_API_KEY", "")

        # Load specific CSV file or latest
        if csv_filename:
            filepath = os.path.join(SRC_DIR, csv_filename)
            if os.path.exists(filepath):
                df = pd.read_csv(filepath)
                filename = csv_filename
            else:
                print(f"[WeeklyAnalysis] Specified file not found: {filepath}")
                return None
        else:
            df, filename = self.load_latest_data()

        if df is None:
            return

        # --- 1. Preprocessing ---
        # Normalize columns
        df.columns = [c.strip() for c in df.columns]
        
        # Filter valid rows
        if '问题关键字' in df.columns:
            df = df.dropna(subset=['问题关键字'])
        else:
             print("Error: '问题关键字' column not found.")
             return
        
        # Basic Metrics
        total_tickets = len(df)

        if total_tickets == 0:
            print("Warning: 本周无工单数据，跳过分析")
            return {"error": "本周无工单数据（0条），无法生成周报"}

        # Projects
        df['Project'] = df['项目名称'].fillna('Unknown')

        process_tickets = df[df['Project'] == self.project_name]
        transferred_tickets = df[df['Project'] != self.project_name]

        count_process = len(process_tickets)
        count_transferred = len(transferred_tickets)
        ratio_transferred = (count_transferred / total_tickets * 100) if total_tickets > 0 else 0

        # --- 2. Stats Analysis ---
        type_col = '自定义字段(研发确认问题类型)'
        if type_col not in df.columns:
            print(f"Warning: 列 '{type_col}' 不存在，使用空分类")
            df[type_col] = '未分类'
        valid_types = df[type_col].fillna('未分类')
        type_counts = valid_types.value_counts(normalize=True) * 100
        
        assignee_col = '经办人'
        assignees = df[assignee_col].fillna('Unknown')
        
        roles = assignees.apply(self.crew_parser.get_role)
        real_names = assignees.apply(self.get_realname)
        
        role_counts = roles.value_counts(normalize=True) * 100
        assignee_counts = real_names.value_counts(normalize=True) * 100
        
        created_col = '创建日期'
        if created_col in df.columns:
            df['Day'] = pd.to_datetime(df[created_col]).dt.date
            daily_counts = df['Day'].value_counts().sort_index()
        else:
            daily_counts = {}

        # --- 3. Categorization & Table Generation ---
        
        def generate_md_table(tkts_df, columns):
            if tkts_df.empty:
                return "*(本周无此类工单)*\n"
            
            # Markdown Table Header
            header = "| " + " | ".join(columns) + " |\n"
            separator = "| " + " | ".join(["---"] * len(columns)) + " |\n"
            
            body = ""
            for _, row in tkts_df.iterrows():
                row_str = "|"
                for col in columns:
                    val = str(row.get(col, '')).replace('\n', ' ').replace('|', '\\|')
                    # Special handling for aliases
                    if col == '问题编号': val = str(row.get('问题关键字', ''))
                    if col == '问题描述': val = str(row.get('概要', ''))[:80]
                    if col == '问题类型': val = str(row.get(type_col, ''))
                    if col == '研发确认问题类型': val = str(row.get(type_col, ''))
                    if col == '经办人': val = self.get_realname(str(row.get('经办人', '')))
                    if col == '创建时间': val = str(row.get('创建日期', ''))[:10]
                    if col == '完成时间': val = str(row.get('解决日期', ''))[:10]
                    if col == '问题用时':
                        try:
                            created = pd.to_datetime(row.get('创建日期'))
                            resolved = pd.to_datetime(row.get('解决日期'))
                            if pd.notna(created) and pd.notna(resolved):
                                delta = resolved - created
                                val = f"{delta.days}天{delta.seconds//3600}时"
                            else:
                                val = "-"
                        except:
                            val = "-"
                    
                    row_str += f" {val} |"
                body += row_str + "\n"
                
            return header + separator + body

        def filter_tickets(criteria_func):
            return df[df.apply(criteria_func, axis=1)]

        # Requirement
        req_df = filter_tickets(lambda r: 
            str(r.get(type_col, '')).find('需求') >= 0 or 
            '需求' in str(r.get('自定义字段(解决方案)', '')) or 
            '不支持' in str(r.get('自定义字段(解决方案)', ''))
        )
        # Sub-chart: Requirement Status/Type
        req_counts = req_df[type_col].value_counts(normalize=True) * 100
        
        # Operation
        op_df = filter_tickets(lambda r: 
            str(r.get(type_col, '')).find('应用操作') >= 0 or 
            '操作' in str(r.get('自定义字段(解决方案)', '')) or 
            '设置' in str(r.get('自定义字段(解决方案)', ''))
        )
        # Sub-chart: Operation Assignee
        op_assignees = op_df[assignee_col].fillna('Unknown').apply(self.get_realname)
        op_counts = op_assignees.value_counts(normalize=True) * 100
        
        # Implementation
        impl_df = filter_tickets(lambda r: 
            str(r.get(type_col, '')).find('实施') >= 0 or 
            '实施' in str(r.get('自定义字段(解决方案)', ''))
        )
        
        # Ops
        ops_df = filter_tickets(lambda r: 
            str(r.get(type_col, '')).find('运维') >= 0 or 
            '运维' in str(r.get('自定义字段(解决方案)', ''))
        )

        process_labeled = get_process_labeled_issues(df=df, label_pattern="流程-")

        # Transferred
        transfer_df = transferred_tickets
        # Sub-chart: Transferred Project
        trans_counts = transfer_df['Project'].value_counts(normalize=True) * 100

        # --- 4. Load Previous Week Data for Comparison ---
        prev_report = self.load_previous_week_report()
        prev_meta = prev_report.get('meta', {})
        prev_charts = prev_report.get('charts', {})
        
        prev_total = prev_meta.get('total_tickets', 0)
        prev_process = prev_meta.get('count_process', 0)
        prev_transferred = prev_meta.get('count_transferred', 0)
        prev_type_counts = prev_charts.get('type_counts', {})
        prev_role_counts = prev_charts.get('role_counts', {})
        prev_assignee_counts = prev_charts.get('assignee_counts', {})

        # --- 4.5 KPI Analysis ---
        # 提前计算数据日期范围 (用于KPI同比)
        _date_match = re.search(r'(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})', filename)
        if _date_match:
            _data_start = _date_match.group(1)
            _data_end = _date_match.group(2)
        else:
            try:
                _data_start = pd.to_datetime(df['创建日期']).min().strftime('%Y-%m-%d')
                _data_end = pd.to_datetime(df['创建日期']).max().strftime('%Y-%m-%d')
            except:
                _data_start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
                _data_end = datetime.now().strftime('%Y-%m-%d')

        kpi_calc = KPICalculator()
        kpi_current = kpi_calc.calculate_period_kpi(df)

        # 加载去年同周数据
        kpi_last_year = {}
        last_year_df = kpi_calc.load_last_year_same_week(_data_start, _data_end)
        if last_year_df is not None:
            kpi_last_year = kpi_calc.calculate_period_kpi(last_year_df)

        # 上周KPI (从上周报告)
        kpi_prev = prev_report.get('kpi_analysis', {}).get('current', {})

        # 同比/环比
        kpi_yoy = kpi_calc.calculate_yoy_kpi(kpi_current, kpi_last_year) if kpi_last_year else {}
        kpi_mom = kpi_calc.calculate_mom_kpi(kpi_current, kpi_prev) if kpi_prev else {}

        # 不达标客户
        kpi_non_compliant = kpi_calc.get_non_compliant_customers(df)
        kpi_distribution = kpi_calc.get_customer_distribution_bands(df)
        kpi_trend = kpi_calc.get_weekly_kpi_trend(weeks=8)

        # YTD KPI (年初至今, 精确去重)
        kpi_ytd = kpi_calc.calculate_ytd_from_csv(
            year=datetime.now().year,
            end_date=_data_end
        )

        # 未来8周YTD预测（Holt线性趋势平滑）
        kpi_forecast = kpi_calc.forecast_weekly_ytd(
            trend_data=kpi_trend,
            current_ytd=kpi_ytd or {},
            weeks_ahead=8,
        )

        kpi_data = {
            "current": kpi_current,
            "last_year": kpi_last_year,
            "last_period": kpi_prev,
            "yoy": kpi_yoy,
            "mom": kpi_mom,
            "non_compliant": kpi_non_compliant,
            "distribution": kpi_distribution,
            "trend": kpi_trend,
            "forecast": kpi_forecast,
            "threshold": kpi_calc.weekly_threshold,
        }

        # --- 5. Generate Report Content ---

        report_content = f"# 周总结分析报告\n\n"
        report_content += f"**数据来源**: `{filename}`\n"
        report_content += f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        if prev_report:
            report_content += f"**对比数据**: 上周报告 (`{prev_meta.get('filename', 'N/A')}`)\n"
        report_content += "\n"
        
        # --- 5.1 Total Overview with Comparison ---
        report_content += "## 1. 工单总数概览\n"
        
        def format_count_compare(current, previous, label):
            if previous > 0:
                change = ((current - previous) / previous) * 100
                arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
                return f"- **{label}**: {current} ({arrow}{abs(change):.1f}% 环比)\n"
            return f"- **{label}**: {current}\n"
        
        report_content += format_count_compare(total_tickets, prev_total, "接收工单总数")
        report_content += format_count_compare(count_process, prev_process, "流程工单数 (核心)")
        report_content += format_count_compare(count_transferred, prev_transferred, "转出工单数")
        report_content += f"- **转出占比**: {self._format_compare(ratio_transferred, prev_meta.get('ratio_transferred', 0))}\n\n"

        # --- 5.1.5 KPI Analysis Section ---
        report_content += kpi_calc.generate_kpi_section_md(kpi_data, report_type="weekly")

        # --- 5.2 Distribution Analysis with Comparison ---
        report_content += "## 3. 工单占比分析\n"
        report_content += "### 问题类型占比\n"
        report_content += "| 类型 | 本周占比 | 上周占比 | 环比变化 |\n|------|---------|---------|----------|\n"
        for t, p in type_counts.items():
            prev_p = prev_type_counts.get(t, 0)
            change = p - prev_p
            arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
            report_content += f"| {t} | {p:.1f}% | {prev_p:.1f}% | {arrow}{abs(change):.1f}% |\n"
        
        report_content += "\n### 处理角色占比\n"
        report_content += "| 角色 | 本周占比 | 上周占比 | 环比变化 |\n|------|---------|---------|----------|\n"
        for r, p in role_counts.items():
            prev_p = prev_role_counts.get(r, 0)
            change = p - prev_p
            arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
            report_content += f"| {r} | {p:.1f}% | {prev_p:.1f}% | {arrow}{abs(change):.1f}% |\n"
        
        report_content += "\n"

        # --- 5. AI & Tables ---
        
        # Helper for prompting
        def format_for_llm(tkts_df, limit=50):
            lines = []
            for _, row in tkts_df.head(limit).iterrows():
                key = row['问题关键字']
                summary = row['概要']
                sol = str(row.get('自定义字段(解决方案)', ''))[:100].replace('\n', ' ')
                lines.append(f"- [{key}] {summary} (Solution: {sol})")
            return "\n".join(lines)

        # AI Function
        def get_ai_summary(prompt, default_msg="*(未能生成AI总结)*"):
            if not api_key:
                return default_msg + " (未配置API Key)"
            try:
                # Pass all config
                return self.llm_service.call_llm(
                    prompt, 
                    api_key=api_key, 
                    provider=provider, 
                    model_name=model_name, 
                    base_url=base_url
                )
            except Exception as e:
                return f"*(AI分析出错: {str(e)})*"

        # A. Top 10 Focus with Clustering (AI)
        report_content += "## 4. 工单重点关注 (TOP 10 聚类)\n"
        
        # Include tickets with labels (标签)
        label_col = '标签'
        labeled_tickets = df[df[label_col].notna() & (df[label_col] != '')] if label_col in df.columns else pd.DataFrame()
        
        if api_key:
            focus_prompt = f"""你是一个工单分析专家，目标是帮助团队**降低工单数量**。请根据以下工单列表，进行自动聚类分析，识别本周最值得关注的TOP 10个主要问题集群。

## 分析要求

**一、聚类分析表格**（必须输出）
输出格式为Markdown表格：
| 排名 | 问题集群名称 | 工单数 | 行动类型 | 责任方 | 预计降量/周 |

其中**行动类型**必须从以下四类中选一类：
- **A-用户培训**：问题根因是用户操作错误或不理解某功能，通过培训/文档/FAQ可消灭
- **B-顾问赋能**：问题根因是技术顾问不知道标准解法，通过知识库沉淀/方案赋能可消灭
- **C-诊断工具**：问题需要后台查日志/操作数据库/研发介入才能解，需要开发自助诊断工具
- **D-产品规划**：问题是产品功能缺失或设计不合理，需要产品设计并研发实现

**二、各聚类深度分析**
对每个集群给出：
- 问题描述和典型工单编号
- 行动标签原因说明（为什么选这个类型）
- 具体可执行的下一步行动（1句话，要具体）

**三、本周行动摘要**（必须输出，放最后）
输出一个"本周可执行行动清单"，格式：
| 优先级 | 行动描述 | 责任方 | 预计减少工单 |
只列P1和P2，最多8条，每条行动要具体（不能写"优化XX"，要写"做XX，输出YY"）。

带标签的重点工单（必须分析）：
{format_for_llm(labeled_tickets, limit=20)}

全部工单列表：
{format_for_llm(df, limit=80)}
"""
            report_content += get_ai_summary(focus_prompt) + "\n\n"
        else:
             report_content += "> *需配置API Key以生成智能重点关注分析* \n\n"

        # B. Requirements - with topic classification
        report_content += "## 5. 需求类工单分析\n"
        report_content += "### AI 按主题总结\n"
        req_prompt = f"""基于以下需求类工单，请按照主题进行分类总结：

主题结构参考：
- 工作流产品结构（流程引擎、字段权限、工作流设计器等）
- 工作流上游业务（UI模板、业务对象等）
- 工作流平行业务（消息模板、业务活动等）
- 工作流下游业务（单据、消息中心等）

请分析：
1. 各主题下的需求分布
2. 纳入/计划解决的需求
3. 拒绝的需求及理由

工单列表：
{format_for_llm(req_df)}"""
        report_content += get_ai_summary(req_prompt) + "\n\n"

        if process_labeled:
            report_content += "### \"流程-\"标签重点关注问题\n\n"
            report_content += f"*本周共有 {len(process_labeled)} 个带\"流程-\"标签的工单*\n\n"
            report_content += "| 工单编号 | 问题描述 | 问题类型 | 标签 |\n"
            report_content += "|---------|---------|---------|------|\n"
            for ticket in process_labeled[:20]:
                key = ticket.get('问题关键字', 'N/A')
                summary = str(ticket.get('概要', ''))[:50].replace('\n', ' ').replace('|', '\\|')
                issue_type = ticket.get('自定义字段(研发确认问题类型)', '-') or ticket.get('研发确认问题类型', '-')
                label = ticket.get('matched_label', '-') or ticket.get('标签', '-')
                report_content += f"| {key} | {summary} | {issue_type} | {label} |\n"
            if len(process_labeled) > 20:
                report_content += f"\n*...共 {len(process_labeled)} 个工单，仅显示前 20 个*\n"
            report_content += "\n"

        report_content += "### 详细清单\n"
        report_content += generate_md_table(req_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时']) + "\n"
        
        # C. Operations - with topic classification
        report_content += "## 6. 操作类工单分析\n"
        report_content += "### AI 按主题总结\n"
        op_prompt = f"""基于以下操作/咨询类工单，请按照主题分类总结：

请分析：
1. 各主题下的操作问题分布
2. 用户操作错误的问题有哪些
3. 用户不明白某个设置的问题有哪些
4. 产品改进建议

工单列表：
{format_for_llm(op_df)}"""
        report_content += get_ai_summary(op_prompt) + "\n\n"
        report_content += "### 详细清单\n"
        report_content += generate_md_table(op_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时']) + "\n"

        # D. Transferred
        report_content += "## 7. 转出类工单分析\n"
        report_content += "### AI 总结\n"
        trans_prompt = f"""基于以下转出工单，请分析转出情况：

请分析：
1. 主要转出问题总结
2. 按项目名称的工单问题分布
3. 主要转出项目是哪些
4. 这些项目的问题聚焦在哪里

工单列表：
{format_for_llm(transfer_df)}"""
        report_content += get_ai_summary(trans_prompt) + "\n\n"
        report_content += "### 详细清单\n"
        report_content += generate_md_table(transfer_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时']) + "\n"
        
        # E. Implementation 
        report_content += "## 8. 实施类工单分析\n"
        report_content += "### AI 按主题总结\n"
        impl_prompt = f"""基于以下实施类工单，请按主题分类进行总结：

请分析：
1. 实施类问题按主题分布
2. 用户实施的主要问题有哪些
3. 常见问题归纳
4. 改进建议

工单列表：
{format_for_llm(impl_df)}"""
        report_content += get_ai_summary(impl_prompt) + "\n\n"
        report_content += "### 详细清单\n"
        report_content += generate_md_table(impl_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时']) + "\n"

        # F. Ops
        report_content += "## 9. 运维类工单分析\n"
        report_content += "### AI 按主题总结\n"
        ops_prompt = f"""基于以下运维类工单，请按主题分类进行总结：

请分析：
1. 运维类问题按主题分布
2. 用户遭遇的运维问题有哪些
3. 常见问题归纳
4. 改进建议

工单列表：
{format_for_llm(ops_df)}"""
        report_content += get_ai_summary(ops_prompt) + "\n\n"
        report_content += "### 详细清单\n"
        report_content += generate_md_table(ops_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时']) + "\n"

        # --- Extract data start and end dates from CSV filename or data ---
        # Try to extract date range from filename (new format: 工作流-周数据-2026-01-18-2026-01-25...)
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})', filename)
        if date_match:
            data_start_date = date_match.group(1)
            data_end_date = date_match.group(2)
        else:
            # Fallback: infer from DataFrame's creation dates
            try:
                data_start_date = pd.to_datetime(df['创建日期']).min().strftime('%Y-%m-%d')
                data_end_date = pd.to_datetime(df['创建日期']).max().strftime('%Y-%m-%d')
            except:
                # Final fallback: use current date
                today = datetime.now()
                data_start_date = (today - timedelta(days=7)).strftime('%Y-%m-%d')
                data_end_date = today.strftime('%Y-%m-%d')

        # --- Generate unified report filename based on data date range ---
        filename_base = f"Weekly_Report_{data_start_date}_{data_end_date}"

        # --- Check if report already exists (unless force=True) ---
        if not force:
            existing_json = os.path.join(self.report_dir, f"{filename_base}.json")
            if os.path.exists(existing_json):
                print(f"[WeeklyAnalysis] Report already exists: {filename_base}.json (use force=True to regenerate)")
                return {
                    "status": "exists",
                    "filename": f"{filename_base}.json",
                    "data_start_date": data_start_date,
                    "data_end_date": data_end_date
                }

        # --- Write Report (Markdown) ---
        out_filename_md = f"{filename_base}.md"
        out_path_md = os.path.join(self.report_dir, out_filename_md)
        with open(out_path_md, 'w', encoding='utf-8') as f:
            f.write(report_content)
        print(f"Report Generated (MD): {out_path_md}")

        # --- Prepare ticket details for monthly report aggregation ---
        def extract_ticket_details(tkts_df):
            """提取工单明细，供月报聚合使用"""
            tickets = []
            for _, row in tkts_df.iterrows():
                # Calculate time spent
                time_spent = "-"
                try:
                    created = pd.to_datetime(row.get('创建日期'))
                    resolved = pd.to_datetime(row.get('解决日期'))
                    if pd.notna(created) and pd.notna(resolved):
                        delta = resolved - created
                        time_spent = f"{delta.days}天{delta.seconds//3600}时"
                except:
                    pass

                tickets.append({
                    '问题关键字': str(row.get('问题关键字', '')),
                    '概要': str(row.get('概要', ''))[:100],  # 限制长度
                    '经办人': self.get_realname(str(row.get('经办人', ''))),
                    '研发确认问题类型': str(row.get(type_col, '')),
                    '解决方案': str(row.get('自定义字段(解决方案)', ''))[:200],  # 限制长度
                    '回复方式': str(row.get('自定义字段(回复方式)', '')),
                    '标签': str(row.get('标签', '')),
                    '创建日期': str(row.get('创建日期', ''))[:10],
                    '解决日期': str(row.get('解决日期', ''))[:10],
                    '问题用时': time_spent,
                    '项目名称': str(row.get('项目名称', '')),
                    '客户名称': str(row.get('自定义字段(项目名称)', ''))
                })
            return tickets

        # --- Write Report (JSON for Frontend) ---
        import json
        json_data = {
            "meta": {
                "source_file": filename,
                "filename": filename,
                "generated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "data_start_date": data_start_date,  # 新增：数据起始日期
                "data_end_date": data_end_date,      # 新增：数据结束日期
                "period": f"{data_start_date} 至 {data_end_date}",  # 新增：数据周期
                "total_tickets": total_tickets,
                "count_process": count_process,
                "count_transferred": count_transferred,
                "ratio_transferred": ratio_transferred,
                "has_labeled_issues": len(process_labeled) > 0,
            },
            "charts": {
                "type_counts": type_counts.to_dict(),
                "role_counts": role_counts.to_dict(),
                "assignee_counts": assignee_counts.to_dict(),
                "daily_counts": {str(k): v for k, v in daily_counts.items()},
                "daily_customer_density": kpi_calc.calculate_daily_customer_density(df),
                # Sub-charts
                "req_counts": req_counts.to_dict(),
                "trans_counts": trans_counts.to_dict(),
                "op_counts": op_counts.to_dict()
            },
            # Ticket details for monthly report aggregation
            "ticket_details": {
                "requirement_tickets": extract_ticket_details(req_df),
                "operation_tickets": extract_ticket_details(op_df),
                "transferred_tickets": extract_ticket_details(transfer_df),
                "implementation_tickets": extract_ticket_details(impl_df),
                "ops_tickets": extract_ticket_details(ops_df),
                "all_tickets": extract_ticket_details(df)  # 全部工单，用于去重验证
            },
            "labeled_issues": {
                "process_labeled": process_labeled
            },
            "kpi_analysis": {
                "current": {k: v for k, v in kpi_current.items() if k != "customer_breakdown"},
                "last_year_same_week": {k: v for k, v in kpi_last_year.items() if k != "customer_breakdown"} if kpi_last_year else {},
                "previous_week": kpi_prev if kpi_prev else {},
                "yoy_change_pct": kpi_yoy.get("change_pct"),
                "mom_change_pct": kpi_mom.get("change_pct"),
                "target": kpi_calc.target,
                "gap": kpi_yoy.get("gap", round(kpi_current.get("per_customer", 0) - kpi_calc.target, 2)),
                "customer_distribution": kpi_distribution,
                "weekly_trend": kpi_trend,
                "non_compliant_customers": [{k: v for k, v in nc.items()} for nc in kpi_non_compliant[:20]],
                "ytd": {k: v for k, v in kpi_ytd.items() if k != "customer_breakdown"} if kpi_ytd else {},
                "baseline_per_customer": kpi_calc.baseline_per_customer,
            },
            "content": report_content
        }

        out_filename_json = f"{filename_base}.json"
        out_path_json = os.path.join(self.report_dir, out_filename_json)
        with open(out_path_json, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

        print(f"Report Generated (JSON): {out_path_json}")

        return {
            "status": "success",
            "filename": f"{filename_base}.json",
            "data_start_date": data_start_date,
            "data_end_date": data_end_date,
            "total_tickets": total_tickets
        }

if __name__ == "__main__":
    analyzer = WeeklyAnalyzer()
    analyzer.run()
