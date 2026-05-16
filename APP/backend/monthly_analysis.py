"""
月报分析模块 - 基于周报数据生成月报，支持去年同期对比
"""

import os
import json
import re
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from collections import Counter, defaultdict
import pandas as pd

# 使用绝对路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
CONCLUSION_DIR = os.path.join(PROJECT_ROOT, "conclusion")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
REPORT_DIR = os.path.join(CONCLUSION_DIR, "WeeklyReports")
CREWLIST_PATH = os.path.join(PROJECT_ROOT, "_local/notes/crewlist.md")
TOPIC_PATH = os.path.join(BASE_DIR, "data", "topic.md")

from weekly_analysis import WeeklyAnalyzer
from analysis import CrewListParser, TopicParser, PROJECT_DISPLAY_NAMES
from llm_service import LLMService
from vector_store import VectorStore
from kpi_calculator import KPICalculator

# 月报存储目录
MONTHLY_REPORT_DIR = os.path.join(CONCLUSION_DIR, "MonthlyReports")
os.makedirs(MONTHLY_REPORT_DIR, exist_ok=True)

# LLM配置文件路径
LLM_CONFIG_FILE = os.path.join(BASE_DIR, "llm_config.json")


def load_llm_config() -> Dict:
    """加载LLM配置，复用问题分析模块的配置"""
    if not os.path.exists(LLM_CONFIG_FILE):
        return {}
    try:
        with open(LLM_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[MonthlyReport] 加载LLM配置失败: {e}")
        return {}


def get_llm_credentials() -> Tuple[str, str, str, str]:
    """
    获取LLM凭证，复用全局配置
    返回: (api_key, provider, model_name, base_url)
    """
    config = load_llm_config()
    if not config:
        return "", "gemini", "", ""

    # 获取最后使用的provider
    provider = config.get("last_provider", "gemini")

    # 获取对应provider的配置
    provider_config = config.get(provider, {})
    api_key = provider_config.get("api_key", "")
    model_name = provider_config.get("model_name", "")
    base_url = provider_config.get("base_url", "")

    return api_key, provider, model_name, base_url


class MonthlyAnalyzer:
    """月报分析器 - 聚合周报数据或使用月数据CSV生成月报"""

    def __init__(self, project_key: str = "MYPROJECT", domain_modules: Optional[List[str]] = None):
        self.project_key = project_key
        self.domain_modules = domain_modules or []
        self.project_name = PROJECT_DISPLAY_NAMES.get(project_key, project_key)
        # MYPROJECT 写根目录保持历史兼容；其它项目写子目录
        if project_key == "MYPROJECT":
            self.monthly_report_dir = MONTHLY_REPORT_DIR
            self.weekly_report_dir = REPORT_DIR
        else:
            self.monthly_report_dir = os.path.join(MONTHLY_REPORT_DIR, project_key)
            self.weekly_report_dir = os.path.join(REPORT_DIR, project_key)
        os.makedirs(self.monthly_report_dir, exist_ok=True)
        self.crew_parser = CrewListParser(CREWLIST_PATH)
        self.topic_parser = TopicParser(TOPIC_PATH, project_key)
        self.llm_service = LLMService()
        # Reuse WeeklyAnalyzer's crew parsing logic to avoid duplication
        self.weekly_analyzer = WeeklyAnalyzer(project_key, domain_modules=domain_modules)

    def get_realname(self, username: str) -> str:
        """Get real name from username - delegates to WeeklyAnalyzer"""
        return self.weekly_analyzer.get_realname(username)

    def _get_month_data_csv(self, year: int, month: int) -> Optional[Tuple[pd.DataFrame, str]]:
        """查找指定年月的月数据CSV文件"""
        if not os.path.exists(SRC_DIR):
            return None

        # 查找包含"月数据"且匹配年月模式的文件
        pattern = f"{year}{month:02d}"
        files = [f for f in os.listdir(SRC_DIR)
                 if "月数据" in f and f.endswith(".csv") and pattern in f]

        if not files:
            return None

        # 按文件名排序取最新
        files.sort(reverse=True)
        target_file = files[0]
        filepath = os.path.join(SRC_DIR, target_file)

        try:
            df = pd.read_csv(filepath)
            return df, target_file
        except Exception as e:
            print(f"Error loading monthly CSV: {e}")
            return None

    def _get_weekly_reports_for_month(self, year: int, month: int) -> List[Dict]:
        """获取与指定年月有日期交集的所有周报数据"""
        reports = []

        if not os.path.exists(self.weekly_report_dir):
            return reports

        from datetime import date as _date, timedelta as _td
        import calendar as _cal
        month_start = _date(year, month, 1)
        month_end = _date(year, month, _cal.monthrange(year, month)[1])

        _DATE_PAT = re.compile(r'(\d{4})-(\d{2})-(\d{2})')

        def _parse_date(t):
            return _date(int(t[0]), int(t[1]), int(t[2]))

        for filename in os.listdir(self.weekly_report_dir):
            if not filename.endswith('.json'):
                continue

            week_start = week_end = None

            dates = _DATE_PAT.findall(filename)
            if dates:
                week_start = _parse_date(dates[0])
                week_end = _parse_date(dates[-1]) if len(dates) >= 2 else week_start + _td(days=6)
            else:
                # 旧格式: Weekly_Report_YYYYMMDD.json
                m = re.search(r'Weekly_Report_(\d{8})', filename)
                if m:
                    ds = m.group(1)
                    week_start = _date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
                    week_end = week_start + _td(days=6)
                else:
                    # 从 JSON 内容读取 generated_at
                    try:
                        with open(os.path.join(self.weekly_report_dir, filename), 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            generated_at = data.get('meta', {}).get('generated_at', '')
                            m2 = re.search(r'(\d{4})-(\d{2})-(\d{2})', generated_at)
                            if m2:
                                week_start = _date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
                                week_end = week_start + _td(days=6)
                    except Exception:
                        continue

            if week_start is None:
                continue

            # 交集判断：week_end >= month_start && week_start <= month_end
            if week_end < month_start or week_start > month_end:
                continue

            try:
                with open(os.path.join(self.weekly_report_dir, filename), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    data['_filename'] = filename
                    data['_date'] = week_start.strftime('%Y%m%d')
                    reports.append(data)
            except Exception as e:
                print(f"Error loading weekly report {filename}: {e}")

        reports.sort(key=lambda x: x['_date'])
        return reports

    def _aggregate_weekly_reports(self, reports: List[Dict], year: int = None, month: int = None) -> Dict:
        """聚合多个周报数据为月报数据，可按自然月过滤跨月边界工单"""
        if not reports:
            return None

        # 基础指标聚合
        total_tickets = sum(r['meta'].get('total_tickets', 0) for r in reports)
        count_process = sum(r['meta'].get('count_process', 0) for r in reports)
        count_transferred = sum(r['meta'].get('count_transferred', 0) for r in reports)
        ratio_transferred = (count_transferred / total_tickets * 100) if total_tickets > 0 else 0

        # 图表数据聚合
        type_counts = defaultdict(float)
        role_counts = defaultdict(float)
        assignee_counts = defaultdict(float)
        daily_counts = defaultdict(int)
        req_counts = defaultdict(float)
        trans_counts = defaultdict(float)
        op_counts = defaultdict(float)

        # 工单明细聚合（用于生成详细分析）
        all_requirement_tickets = []
        all_operation_tickets = []
        all_transferred_tickets = []
        all_implementation_tickets = []
        all_ops_tickets = []
        all_tickets = []  # 全部工单，用于TOP10分析

        # 用于去重
        seen_tickets = set()
        seen_all_tickets = set()  # 用于all_tickets去重

        for r in reports:
            charts = r.get('charts', {})

            # 归一化后累加各周占比（简化处理）
            for k, v in charts.get('type_counts', {}).items():
                type_counts[k] += v * r['meta'].get('total_tickets', 0) / total_tickets if total_tickets > 0 else 0

            for k, v in charts.get('role_counts', {}).items():
                role_counts[k] += v * r['meta'].get('total_tickets', 0) / total_tickets if total_tickets > 0 else 0

            for k, v in charts.get('assignee_counts', {}).items():
                assignee_counts[k] += v * r['meta'].get('total_tickets', 0) / total_tickets if total_tickets > 0 else 0

            for k, v in charts.get('daily_counts', {}).items():
                daily_counts[k] += v

            for k, v in charts.get('req_counts', {}).items():
                req_counts[k] += v * r['meta'].get('total_tickets', 0) / total_tickets if total_tickets > 0 else 0

            for k, v in charts.get('trans_counts', {}).items():
                trans_counts[k] += v * r['meta'].get('total_tickets', 0) / total_tickets if total_tickets > 0 else 0

            for k, v in charts.get('op_counts', {}).items():
                op_counts[k] += v * r['meta'].get('total_tickets', 0) / total_tickets if total_tickets > 0 else 0

            # 提取工单明细并去重
            ticket_details = r.get('ticket_details', {})

            for ticket in ticket_details.get('requirement_tickets', []):
                key = ticket.get('问题关键字', '')
                if key and key not in seen_tickets:
                    seen_tickets.add(key)
                    all_requirement_tickets.append(ticket)

            for ticket in ticket_details.get('operation_tickets', []):
                key = ticket.get('问题关键字', '')
                if key and key not in seen_tickets:
                    seen_tickets.add(key)
                    all_operation_tickets.append(ticket)

            for ticket in ticket_details.get('transferred_tickets', []):
                key = ticket.get('问题关键字', '')
                if key and key not in seen_tickets:
                    seen_tickets.add(key)
                    all_transferred_tickets.append(ticket)

            for ticket in ticket_details.get('implementation_tickets', []):
                key = ticket.get('问题关键字', '')
                if key and key not in seen_tickets:
                    seen_tickets.add(key)
                    all_implementation_tickets.append(ticket)

            for ticket in ticket_details.get('ops_tickets', []):
                key = ticket.get('问题关键字', '')
                if key and key not in seen_tickets:
                    seen_tickets.add(key)
                    all_ops_tickets.append(ticket)

            # 聚合全部工单（用于TOP10分析）
            for ticket in ticket_details.get('all_tickets', []):
                key = ticket.get('问题关键字', '')
                if key and key not in seen_all_tickets:
                    seen_all_tickets.add(key)
                    all_tickets.append(ticket)

        # 按自然月过滤跨月边界工单（cross-boundary weeks 会带入相邻月工单）
        if year is not None and month is not None:
            month_prefix = f"{year}-{month:02d}"

            def _in_month(t):
                return str(t.get('创建日期', ''))[:7] == month_prefix

            all_tickets = [t for t in all_tickets if _in_month(t)]
            all_requirement_tickets = [t for t in all_requirement_tickets if _in_month(t)]
            all_operation_tickets = [t for t in all_operation_tickets if _in_month(t)]
            all_transferred_tickets = [t for t in all_transferred_tickets if _in_month(t)]
            all_implementation_tickets = [t for t in all_implementation_tickets if _in_month(t)]
            all_ops_tickets = [t for t in all_ops_tickets if _in_month(t)]

            # 过滤每日统计
            daily_counts = {k: v for k, v in daily_counts.items() if str(k)[:7] == month_prefix}

            # 从过滤后的全量工单重算主指标
            total_tickets = len(all_tickets)
            count_transferred = len([t for t in all_tickets if t.get('项目名称', '') != '云平台-流程中心'])
            count_process = total_tickets - count_transferred
            ratio_transferred = (count_transferred / total_tickets * 100) if total_tickets > 0 else 0

        return {
            'meta': {
                'total_tickets': total_tickets,
                'count_process': count_process,
                'count_transferred': count_transferred,
                'ratio_transferred': ratio_transferred,
                'source_type': 'weekly_aggregate',
                'week_count': len(reports),
                'week_files': [r['_filename'] for r in reports]
            },
            'charts': {
                'type_counts': dict(type_counts),
                'role_counts': dict(role_counts),
                'assignee_counts': dict(assignee_counts),
                'daily_counts': dict(daily_counts),
                'req_counts': dict(req_counts),
                'trans_counts': dict(trans_counts),
                'op_counts': dict(op_counts)
            },
            # 添加工单明细，用于生成详细分析
            'ticket_details': {
                'requirement_tickets': all_requirement_tickets,
                'operation_tickets': all_operation_tickets,
                'transferred_tickets': all_transferred_tickets,
                'implementation_tickets': all_implementation_tickets,
                'ops_tickets': all_ops_tickets,
                'all_tickets': all_tickets  # 全部工单用于TOP10分析
            }
        }

    def _process_monthly_csv(self, df: pd.DataFrame, filename: str) -> Dict:
        """处理月数据CSV，生成统计结果（复用WeeklyAnalyzer逻辑）"""
        # 使用WeeklyAnalyzer的处理逻辑
        # 归一化列名
        df.columns = [c.strip() for c in df.columns]

        # 过滤有效行
        if '问题关键字' in df.columns:
            df = df.dropna(subset=['问题关键字'])

        total_tickets = len(df)

        # 项目分类
        df['Project'] = df['项目名称'].fillna('Unknown')
        process_tickets = df[df['Project'] == '云平台-流程中心']
        transferred_tickets = df[df['Project'] != '云平台-流程中心']

        count_process = len(process_tickets)
        count_transferred = len(transferred_tickets)
        ratio_transferred = (count_transferred / total_tickets * 100) if total_tickets > 0 else 0

        # 问题类型统计
        type_col = '自定义字段(研发确认问题类型)'
        valid_types = df[type_col].fillna('未分类')
        type_counts = valid_types.value_counts(normalize=True) * 100

        # 经办人统计
        assignee_col = '经办人'
        assignees = df[assignee_col].fillna('Unknown')
        roles = assignees.apply(self.crew_parser.get_role)
        real_names = assignees.apply(self.get_realname)

        role_counts = roles.value_counts(normalize=True) * 100
        assignee_counts = real_names.value_counts(normalize=True) * 100

        # 每日统计
        created_col = '创建日期'
        daily_counts = {}
        if created_col in df.columns:
            df['Day'] = pd.to_datetime(df[created_col]).dt.date
            daily_counts = df['Day'].value_counts().sort_index().to_dict()

        # 需求类统计
        req_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('需求') >= 0 or
            '需求' in str(r.get('自定义字段(解决方案)', '')) or
            '不支持' in str(r.get('自定义字段(解决方案)', '')), axis=1)]
        req_counts = req_df[type_col].value_counts(normalize=True) * 100 if not req_df.empty else pd.Series()

        # 操作类统计
        op_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('应用操作') >= 0 or
            '操作' in str(r.get('自定义字段(解决方案)', '')) or
            '设置' in str(r.get('自定义字段(解决方案)', '')), axis=1)]
        op_assignees = op_df[assignee_col].fillna('Unknown').apply(self.get_realname)
        op_counts = op_assignees.value_counts(normalize=True) * 100 if not op_df.empty else pd.Series()

        # 转出项目统计
        trans_counts = transferred_tickets['Project'].value_counts(normalize=True) * 100 if not transferred_tickets.empty else pd.Series()

        # 实施类工单
        impl_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('实施') >= 0 or
            '实施' in str(r.get('自定义字段(解决方案)', '')), axis=1)]

        # 运维类工单
        ops_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('运维') >= 0 or
            '运维' in str(r.get('自定义字段(解决方案)', '')), axis=1)]

        # Helper: 提取工单明细为字典列表
        def extract_ticket_details(tkts_df):
            tickets = []
            for _, row in tkts_df.iterrows():
                # 计算问题用时
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
                    '概要': str(row.get('概要', ''))[:100],
                    '经办人': self.get_realname(str(row.get('经办人', ''))),
                    '研发确认问题类型': str(row.get(type_col, '')),
                    '解决方案': str(row.get('自定义字段(解决方案)', ''))[:200],
                    '创建日期': str(row.get('创建日期', ''))[:10],
                    '解决日期': str(row.get('解决日期', ''))[:10] if pd.notna(row.get('解决日期')) else '',
                    '问题用时': time_spent,
                    '项目名称': str(row.get('项目名称', ''))
                })
            return tickets

        return {
            'meta': {
                'total_tickets': total_tickets,
                'count_process': count_process,
                'count_transferred': count_transferred,
                'ratio_transferred': ratio_transferred,
                'source_type': 'monthly_csv',
                'source_file': filename
            },
            'charts': {
                'type_counts': type_counts.to_dict(),
                'role_counts': role_counts.to_dict(),
                'assignee_counts': assignee_counts.to_dict(),
                'daily_counts': {str(k): v for k, v in daily_counts.items()},
                'req_counts': req_counts.to_dict(),
                'trans_counts': trans_counts.to_dict(),
                'op_counts': op_counts.to_dict()
            },
            'ticket_details': {
                'requirement_tickets': extract_ticket_details(req_df),
                'operation_tickets': extract_ticket_details(op_df),
                'transferred_tickets': extract_ticket_details(transferred_tickets),
                'implementation_tickets': extract_ticket_details(impl_df),
                'ops_tickets': extract_ticket_details(ops_df),
                'all_tickets': extract_ticket_details(df)
            },
            'raw_df': df  # 保留原始数据用于详细分析（兼容旧代码）
        }

    def generate_monthly_data(self, year: int, month: int) -> Dict:
        """生成指定年月的月报数据（混合数据源模式）"""

        # 1. 优先尝试使用月数据CSV
        csv_result = self._get_month_data_csv(year, month)
        if csv_result:
            df, filename = csv_result
            print(f"使用月数据CSV: {filename}")
            result = self._process_monthly_csv(df, filename)
            result['year'] = year
            result['month'] = month
            result['period'] = f"{year}年{month}月"
            return result

        # 2. 回退到周报聚合
        print(f"未找到月数据CSV，使用周报聚合")
        weekly_reports = self._get_weekly_reports_for_month(year, month)

        if not weekly_reports:
            return {
                'year': year,
                'month': month,
                'period': f"{year}年{month}月",
                'error': '未找到该月的数据'
            }

        print(f"找到 {len(weekly_reports)} 份周报，开始聚合...")
        result = self._aggregate_weekly_reports(weekly_reports, year, month)
        result['year'] = year
        result['month'] = month
        result['period'] = f"{year}年{month}月"

        return result


class YoYAnalyzer:
    """同比分析器 - 对比今年和去年同期数据"""

    def __init__(self, llm_service: LLMService = None):
        self.llm_service = llm_service or LLMService()
        self.monthly_analyzer = MonthlyAnalyzer()

    def load_monthly_report(self, year: int, month: int) -> Optional[Dict]:
        """加载指定年月的月报文件，如果不存在则尝试从CSV提取"""
        import glob as _glob
        # 支持带日期后缀的新格式文件名 Monthly_Report_YYYYMM_*.json
        candidates = sorted(_glob.glob(
            os.path.join(MONTHLY_REPORT_DIR, f"Monthly_Report_{year}{month:02d}_*.json")
        ))
        # 回退：兼容老格式 Monthly_Report_YYYYMM.json
        if not candidates:
            legacy = os.path.join(MONTHLY_REPORT_DIR, f"Monthly_Report_{year}{month:02d}.json")
            if os.path.exists(legacy):
                candidates = [legacy]
        if candidates:
            filepath = candidates[-1]  # 取最新一份
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading monthly report {filepath}: {e}")

        # 尝试从CSV文件提取去年同期数据
        print(f"未找到{year}年{month}月的月报JSON，尝试从CSV提取...")
        csv_data = self._extract_data_from_csv(year, month)
        if csv_data:
            print(f"成功从CSV提取{year}年{month}月数据")
            return csv_data

        return None

    def _extract_data_from_csv(self, year: int, month: int) -> Optional[Dict]:
        """从CSV文件提取指定年月的数据用于同比分析"""
        if not os.path.exists(SRC_DIR):
            return None

        # 查找包含年份的CSV文件（如"2025完成"）
        year_pattern = str(year)
        csv_files = [f for f in os.listdir(SRC_DIR)
                     if f.endswith('.csv') and year_pattern in f and '完成' in f]

        if not csv_files:
            return None

        # 使用找到的第一个匹配文件
        csv_file = csv_files[0]
        filepath = os.path.join(SRC_DIR, csv_file)

        try:
            df = pd.read_csv(filepath)
            df.columns = [c.strip() for c in df.columns]

            # 过滤指定月份的数据
            if '创建日期' not in df.columns:
                return None

            df['创建日期'] = pd.to_datetime(df['创建日期'], errors='coerce')
            df = df.dropna(subset=['创建日期'])
            month_df = df[(df['创建日期'].dt.year == year) & (df['创建日期'].dt.month == month)]

            if month_df.empty:
                return None

            # 使用MonthlyAnalyzer的处理逻辑
            result = self.monthly_analyzer._process_monthly_csv(month_df, f"{year}年{month}月@{csv_file}")
            result['meta']['source_type'] = 'yoy_csv_extract'
            result['meta']['extracted_from'] = csv_file
            return result

        except Exception as e:
            print(f"Error extracting data from CSV: {e}")
            return None

    @staticmethod
    def _normalize_role(role: str) -> str:
        """统一角色名称，解决中英文不一致导致的同比失真"""
        mapping = {
            'developer': '开发',
            'dev': '开发',
            'developer ': '开发',
            'product manager': '产品经理',
            'pm': '产品经理',
            'product': '产品经理',
            'tester': '测试',
            'test': '测试',
            'unknown': '其他',
            '未知': '其他',
        }
        return mapping.get(role.strip().lower(), role.strip())

    def calculate_yoy_metrics(self, current: Dict, last_year: Dict) -> Dict:
        """计算同比指标"""
        if not current or not last_year:
            return {'error': '缺少对比数据'}

        c_meta = current.get('meta', {})
        l_meta = last_year.get('meta', {})

        c_charts = current.get('charts', {})
        l_charts = last_year.get('charts', {})

        # 基础指标同比
        def calc_change(current_val, last_val):
            if last_val == 0 or last_val is None:
                return {'change': None, 'percent': None, 'arrow': '→'}
            change = ((current_val - last_val) / last_val) * 100
            arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
            return {
                'current': current_val,
                'last_year': last_val,
                'change': change,
                'percent': f"{abs(change):.1f}%",
                'arrow': arrow
            }

        total_yoy = calc_change(c_meta.get('total_tickets', 0), l_meta.get('total_tickets', 0))
        process_yoy = calc_change(c_meta.get('count_process', 0), l_meta.get('count_process', 0))
        transferred_yoy = calc_change(c_meta.get('count_transferred', 0), l_meta.get('count_transferred', 0))
        ratio_yoy = calc_change(c_meta.get('ratio_transferred', 0), l_meta.get('ratio_transferred', 0))

        # 问题类型分布对比
        type_comparison = []
        c_types = c_charts.get('type_counts', {})
        l_types = l_charts.get('type_counts', {})
        all_types = set(c_types.keys()) | set(l_types.keys())

        for t in all_types:
            c_val = c_types.get(t, 0)
            l_val = l_types.get(t, 0)
            type_comparison.append({
                'type': t,
                'current': c_val,
                'last_year': l_val,
                **calc_change(c_val, l_val)
            })

        # 按当前占比排序
        type_comparison.sort(key=lambda x: x['current'], reverse=True)

        # 处理角色对比（归一化角色名后再聚合，消除中英文不一致导致的同比失真）
        def normalize_role_counts(raw_counts: dict) -> dict:
            merged = defaultdict(float)
            for role, val in raw_counts.items():
                merged[self._normalize_role(role)] += val
            return dict(merged)

        c_roles = normalize_role_counts(c_charts.get('role_counts', {}))
        l_roles = normalize_role_counts(l_charts.get('role_counts', {}))
        all_roles = set(c_roles.keys()) | set(l_roles.keys())

        role_comparison = []
        for r in all_roles:
            c_val = c_roles.get(r, 0)
            l_val = l_roles.get(r, 0)
            role_comparison.append({
                'role': r,
                'current': c_val,
                'last_year': l_val,
                **calc_change(c_val, l_val)
            })

        role_comparison.sort(key=lambda x: x['current'], reverse=True)

        return {
            'total_tickets': total_yoy,
            'count_process': process_yoy,
            'count_transferred': transferred_yoy,
            'ratio_transferred': ratio_yoy,
            'type_comparison': type_comparison[:10],  # TOP10
            'role_comparison': role_comparison[:5]     # TOP5
        }

    def calculate_mom_metrics(self, current: Dict, last_month: Dict) -> Dict:
        """计算环比指标（对比上个月）"""
        if not current or not last_month:
            return {'error': '缺少对比数据'}

        c_meta = current.get('meta', {})
        l_meta = last_month.get('meta', {})

        c_charts = current.get('charts', {})
        l_charts = last_month.get('charts', {})

        # 基础指标环比
        def calc_change(current_val, last_val):
            if last_val == 0 or last_val is None:
                return {'change': None, 'percent': None, 'arrow': '→'}
            change = ((current_val - last_val) / last_val) * 100
            arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
            return {
                'current': current_val,
                'last_month': last_val,
                'change': change,
                'percent': f"{abs(change):.1f}%",
                'arrow': arrow
            }

        total_mom = calc_change(c_meta.get('total_tickets', 0), l_meta.get('total_tickets', 0))
        process_mom = calc_change(c_meta.get('count_process', 0), l_meta.get('count_process', 0))
        transferred_mom = calc_change(c_meta.get('count_transferred', 0), l_meta.get('count_transferred', 0))
        ratio_mom = calc_change(c_meta.get('ratio_transferred', 0), l_meta.get('ratio_transferred', 0))

        # 问题类型分布对比
        type_comparison = []
        c_types = c_charts.get('type_counts', {})
        l_types = l_charts.get('type_counts', {})
        all_types = set(c_types.keys()) | set(l_types.keys())

        for t in all_types:
            c_val = c_types.get(t, 0)
            l_val = l_types.get(t, 0)
            type_comparison.append({
                'type': t,
                'current': c_val,
                'last_month': l_val,
                **calc_change(c_val, l_val)
            })

        type_comparison.sort(key=lambda x: x['current'], reverse=True)

        return {
            'total_tickets': total_mom,
            'count_process': process_mom,
            'count_transferred': transferred_mom,
            'ratio_transferred': ratio_mom,
            'type_comparison': type_comparison[:10]
        }

    def analyze_top_issues_overlap(self, current_content: str, last_year_content: str,
                                     api_key: str = None, provider: str = None, model_name: str = "", base_url: str = "") -> Dict:
        """分析TOP问题的重合度（基于AI）"""
        if not self.llm_service:
            return {'error': 'LLM服务未配置'}

        # 如果没有传入参数，从配置读取
        if not api_key or not provider:
            config_api_key, config_provider, config_model, config_base_url = get_llm_credentials()
            api_key = api_key or config_api_key
            provider = provider or config_provider
            model_name = model_name or config_model
            base_url = base_url or config_base_url

        if not api_key:
            return {'error': '未配置API Key'}

        prompt = f"""你是一名工单分析专家。请对比以下两年同月份的重点工单，分析重合度和趋势变化。

今年月报内容摘要：
{current_content[:3000]}

去年同月月报内容摘要：
{last_year_content[:3000]}

请分析：
1. TOP10问题的重合度（有哪些问题类型或主题在两年都出现）
2. 新增的问题类型或主题
3. 已解决或消失的问题
4. 趋势总结（哪些问题在恶化/改善）

以结构化的JSON格式返回：
{{
  "overlap_issues": ["问题1", "问题2"],
  "new_issues": ["新问题1"],
  "resolved_issues": ["已解决问题1"],
  "trend_summary": "整体趋势总结"
}}"""

        try:
            response = self.llm_service.call_llm(prompt, api_key=api_key, provider=provider, model_name=model_name, base_url=base_url)
            # 尝试解析JSON
            import json
            # 提取JSON部分
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                return json.loads(json_match.group())
            return {'raw_analysis': response}
        except Exception as e:
            return {'error': str(e)}

    def generate_yoy_insights(self, yoy_metrics: Dict, current_period: str, last_year_period: str) -> str:
        """生成同比洞察文字"""
        total = yoy_metrics.get('total_tickets', {})

        if total.get('change') is None:
            return "无法计算同比变化（缺少去年同期数据）"

        change = total.get('change', 0)
        arrow = total.get('arrow', '→')

        insights = f"## 同比分析 ({current_period} vs {last_year_period})\n\n"

        # 总量变化
        if change > 0:
            insights += f"本月工单总量同比增长 **{abs(change):.1f}%**，工作量有所增加。"
        elif change < 0:
            insights += f"本月工单总量同比下降 **{abs(change):.1f}%**，工作量有所减少。"
        else:
            insights += "本月工单总量与去年同期基本持平。"

        # 各类指标变化
        insights += "\n\n### 关键指标变化\n"

        process = yoy_metrics.get('count_process', {})
        insights += f"- **流程工单数**: {process.get('arrow', '→')} {process.get('percent', 'N/A')}\n"

        transferred = yoy_metrics.get('count_transferred', {})
        insights += f"- **转出工单数**: {transferred.get('arrow', '→')} {transferred.get('percent', 'N/A')}\n"

        ratio = yoy_metrics.get('ratio_transferred', {})
        insights += f"- **转出占比**: {ratio.get('arrow', '→')} {ratio.get('percent', 'N/A')}\n"

        # 问题类型变化
        insights += "\n### 问题类型变化TOP5\n"
        type_comp = yoy_metrics.get('type_comparison', [])
        for i, t in enumerate(type_comp[:5]):
            if t.get('change') is not None:
                insights += f"{i+1}. **{t['type']}**: {t['arrow']} {t['percent']}\n"

        return insights


class MonthlyReportGenerator:
    """月报生成器 - 整合数据生成、同比分析和报告输出"""

    def __init__(self, project_key: str = "MYPROJECT", domain_modules: Optional[List[str]] = None):
        self.project_key = project_key
        self.domain_modules = domain_modules or []
        self.monthly_analyzer = MonthlyAnalyzer(project_key, domain_modules=domain_modules)
        self.yoy_analyzer = YoYAnalyzer()

    def _get_process_labeled_issues(self, df: pd.DataFrame = None, tickets: List[Dict] = None) -> List[Dict]:
        labeled_tickets = [t for t in (tickets or []) if any(
            str(t.get('标签', '')).find('流程-') >= 0 for _ in [1]
        )]
        if labeled_tickets:
            print(f"[MonthlyReport] 获取到 {len(labeled_tickets)} 个带'流程-'标签的工单")
        return labeled_tickets

    def generate(self, year: int, month: int, force: bool = False,
                 api_key: str = None, provider: str = "gemini",
                 model_name: str = "", base_url: str = "") -> Dict:
        """生成完整月报，自动复用全局LLM配置"""

        print(f"开始生成 {year}年{month}月 的月报...")

        # 0. 检查是否已存在（除非force=True）
        if not force:
            # 尝试找到该年月的现有报告
            existing_reports = list_monthly_reports()
            for report in existing_reports:
                if report['year'] == year and report['month'] == month:
                    print(f"[MonthlyReport] Report already exists: {report['filename']} (use force=True to regenerate)")
                    return {
                        "status": "exists",
                        **report
                    }

        # 1. 加载全局LLM配置（作为fallback）
        config_api_key, config_provider, config_model, config_base_url = get_llm_credentials()

        # 优先使用传入的参数，如果没有传入则使用全局配置
        api_key = api_key or config_api_key
        provider = provider or config_provider
        model_name = model_name or config_model
        base_url = base_url or config_base_url

        if api_key:
            print(f"已加载LLM配置: provider={provider}")
        else:
            print("警告: 未找到LLM配置，AI分析功能将不可用")

        # 2. 生成本月数据
        monthly_data = self.monthly_analyzer.generate_monthly_data(year, month)

        if 'error' in monthly_data:
            return monthly_data

        # 3. 加载去年同期数据
        last_year = year - 1
        last_year_data = self.yoy_analyzer.load_monthly_report(last_year, month)

        # 3.5 加载上月数据（环比分析）
        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1
        last_month_data = self.yoy_analyzer.load_monthly_report(prev_year, prev_month)

        # 4. 计算同比指标
        yoy_metrics = {}
        if last_year_data:
            print("计算同比指标...")
            yoy_metrics = self.yoy_analyzer.calculate_yoy_metrics(monthly_data, last_year_data)

        # 4.5 计算环比指标
        mom_metrics = {}
        if last_month_data:
            print("计算环比指标...")
            mom_metrics = self.yoy_analyzer.calculate_mom_metrics(monthly_data, last_month_data)

        # 4.6 KPI分析
        kpi_calc = KPICalculator()
        kpi_data = {}

        # 5. 获取原始数据（用于详细分析）
        raw_df = monthly_data.get('raw_df')
        ticket_details = monthly_data.get('ticket_details', {})
        source_type = monthly_data.get('meta', {}).get('source_type', '')

        # 6. 生成各类工单的详细清单表格（CSV模式）
        detailed_tables = ""
        if raw_df is not None and not raw_df.empty:
            print("生成详细工单清单...")
            detailed_tables = self._generate_detailed_tables(raw_df, api_key, provider, model_name, base_url)

        # 7. 生成TOP10聚类分析（使用AI）- 支持CSV模式和周报聚合模式
        top10_analysis = ""
        if api_key:
            # 合并所有工单用于TOP10分析
            all_tickets = ticket_details.get('all_tickets', [])
            if raw_df is not None and not raw_df.empty:
                # CSV模式：使用DataFrame
                print("生成TOP10聚类分析(CSV模式)...")
                top10_analysis = self._generate_top10_focus(raw_df, api_key, provider, model_name, base_url)
            elif all_tickets:
                # 周报聚合模式：使用ticket_details
                print("生成TOP10聚类分析(周报聚合模式)...")
                top10_analysis = self._generate_top10_focus_from_tickets(all_tickets, api_key, provider, model_name, base_url)

        # 7.1 生成跨周持续性分析
        cross_week_analysis = ""
        if api_key:
            print("生成跨周持续性分析...")
            cross_week_analysis = self._analyze_cross_week_patterns(year, month, api_key, provider, model_name, base_url)

        # 7.2 生成下月行动计划
        next_month_plan = ""
        if api_key and top10_analysis:
            print("生成下月行动计划...")
            next_month_plan = self._generate_next_month_action_plan(
                year, month, top10_analysis, cross_week_analysis,
                api_key, provider, model_name, base_url
            )

        # 7.5 获取标签工单数据
        labeled_issues = None

        # 获取本月所有工单ID
        all_ticket_keys = []
        if raw_df is not None and not raw_df.empty:
            # CSV模式
            issue_key_col = '问题关键字'
            if issue_key_col in raw_df.columns:
                all_ticket_keys = raw_df[issue_key_col].astype(str).tolist()
        elif all_tickets:
            # 周报聚合模式
            all_ticket_keys = [t.get('问题关键字', '') for t in all_tickets if t.get('问题关键字')]

        # 周报聚合模式：尝试加载原始CSV数据进行需求库和标签筛选
        # 因为旧的周报JSON可能缺少"回复方式"和"标签"字段
        source_df = raw_df
        if source_df is None and source_type == 'weekly_aggregate':
            # 尝试从周报meta中获取源CSV文件并加载
            # meta.week_files存储的是周报JSON文件名，需要从周报JSON内容中获取source_file
            reports_data = monthly_data.get('_raw_reports', [])
            if not reports_data:
                # 如果没有原始报告数据，尝试从文件名推断CSV文件
                week_files = monthly_data.get('meta', {}).get('week_files', [])
                for week_file in week_files:
                    # 尝试从JSON文件读取source_file
                    json_path = os.path.join(self.weekly_report_dir, week_file)
                    if os.path.exists(json_path):
                        try:
                            with open(json_path, 'r', encoding='utf-8') as f:
                                report_data = json.load(f)
                                reports_data.append(report_data)
                        except Exception as e:
                            print(f"[MonthlyReport] 读取周报JSON {week_file} 失败: {e}")

            if reports_data:
                print("周报聚合模式：尝试加载原始CSV数据进行需求库和标签筛选...")
                combined_df = None
                for report in reports_data:
                    # 从周报meta获取原始CSV文件名
                    csv_filename = report.get('meta', {}).get('source_file', '')
                    if csv_filename:
                        csv_path = os.path.join(SRC_DIR, csv_filename)
                        if os.path.exists(csv_path):
                            try:
                                week_df = pd.read_csv(csv_path)
                                week_df.columns = [c.strip() for c in week_df.columns]
                                if combined_df is None:
                                    combined_df = week_df
                                else:
                                    combined_df = pd.concat([combined_df, week_df], ignore_index=True)
                                print(f"[MonthlyReport] 加载CSV: {csv_filename}, {len(week_df)} 条记录")
                            except Exception as e:
                                print(f"[MonthlyReport] 加载CSV {csv_filename} 失败: {e}")
                if combined_df is not None:
                    source_df = combined_df
                    print(f"[MonthlyReport] 成功合并 {len(source_df)} 条CSV记录用于需求库和标签筛选")

        # 获取标签工单数据 - 统一使用新方法
        print("筛选标签工单...")
        process_labeled = self._get_process_labeled_issues(df=source_df, tickets=all_tickets)
        labeled_issues = {'process_labeled': process_labeled}

        # 7.6 KPI计算
        print("计算KPI指标...")
        kpi_source_df = source_df if source_df is not None else raw_df
        if kpi_source_df is not None and not kpi_source_df.empty:
            kpi_data["current"] = kpi_calc.calculate_period_kpi(kpi_source_df)
        else:
            kpi_data["current"] = {"total_issues": 0, "unique_customers": 0, "per_customer": 0}

        # 去年同月KPI
        last_year_kpi_df = kpi_calc.load_last_year_same_month(year, month)
        kpi_last_year = {}
        if last_year_kpi_df is not None:
            kpi_last_year = kpi_calc.calculate_period_kpi(last_year_kpi_df)
        kpi_data["last_year"] = kpi_last_year

        # 上月KPI (从上月报告)
        kpi_last_month = {}
        prev_month_num = month - 1 if month > 1 else 12
        prev_month_year = year if month > 1 else year - 1
        prev_monthly_kpi = kpi_calc._load_monthly_kpi(prev_month_year, prev_month_num)
        if prev_monthly_kpi:
            kpi_last_month = prev_monthly_kpi
        kpi_data["last_month"] = kpi_last_month

        # 同比/环比
        kpi_data["yoy"] = kpi_calc.calculate_yoy_kpi(kpi_data["current"], kpi_last_year) if kpi_last_year else {}
        kpi_data["mom"] = kpi_calc.calculate_mom_kpi(kpi_data["current"], kpi_last_month) if kpi_last_month else {}

        # 不达标客户、客户分布
        if kpi_source_df is not None and not kpi_source_df.empty:
            kpi_data["non_compliant"] = kpi_calc.get_non_compliant_customers(kpi_source_df)
            kpi_data["distribution"] = kpi_calc.get_customer_distribution_bands(kpi_source_df)
        else:
            kpi_data["non_compliant"] = []
            kpi_data["distribution"] = []

        # 月度趋势和YTD
        kpi_data["trend"] = kpi_calc.get_monthly_kpi_trend(year)
        kpi_data["ytd"] = kpi_calc.get_ytd_progress(year, month)
        kpi_data["threshold"] = kpi_calc.weekly_threshold
        # 剩余月份预测（YoY比例法 + 2025季节基线）
        kpi_data["forecast"] = kpi_calc.forecast_monthly_remaining(
            monthly_trend=kpi_data["trend"],
            year=year,
        )

        # 每日每客户密度 (用于趋势图)
        if kpi_source_df is not None and not kpi_source_df.empty:
            kpi_data["daily_customer_density"] = kpi_calc.calculate_daily_customer_density(kpi_source_df)
        else:
            kpi_data["daily_customer_density"] = {}

        # 8. 生成报告内容
        report_content = self._generate_report_content(
            monthly_data, yoy_metrics, mom_metrics, last_year_data, last_month_data, year, month,
            top10_analysis, detailed_tables,
            api_key, provider, model_name, base_url,
            labeled_issues=labeled_issues,
            cross_week_analysis=cross_week_analysis,
            next_month_plan=next_month_plan,
            kpi_data=kpi_data
        )

        # 9. 如果有API Key，生成AI同比结论
        ai_yoy_insights = ""
        if api_key and last_year_data:
            print("生成AI同比洞察...")
            ai_yoy_insights = self._generate_ai_yoy_insights(
                monthly_data, last_year_data, api_key, provider, model_name, base_url
            )

        # 9. 数据覆盖范围始终用自然月边界（文件名和 meta 始终反映整月）
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        data_start_date = f"{year}-{month:02d}-01"
        data_end_date   = f"{year}-{month:02d}-{last_day:02d}"

        # 10. 组装输出
        result = {
            'meta': {
                'year': year,
                'month': month,
                'period': f"{year}年{month}月",
                'data_start_date': data_start_date,  # 新增：数据起始日期
                'data_end_date': data_end_date,      # 新增：数据结束日期
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                **monthly_data.get('meta', {}),
                'has_yoy_data': last_year_data is not None,
                'last_year_period': f"{last_year}年{month}月" if last_year_data else None,
                'has_mom_data': last_month_data is not None,
                'last_month_period': f"{prev_year}年{prev_month}月" if last_month_data else None,
                'has_detailed_analysis': raw_df is not None,
                'has_labeled_issues': labeled_issues is not None and len(labeled_issues.get('process_labeled', [])) > 0
            },
            'charts': {**monthly_data.get('charts', {}), 'daily_customer_density': kpi_data.get('daily_customer_density', {})},
            'yoy_analysis': yoy_metrics,
            'mom_analysis': mom_metrics,
            'yoy_insights': ai_yoy_insights if ai_yoy_insights else self.yoy_analyzer.generate_yoy_insights(
                yoy_metrics, f"{year}年{month}月", f"{last_year}年{month}月"
            ) if last_year_data else "",
            'labeled_issues': labeled_issues,
            'kpi_analysis': {
                'current': {k: v for k, v in kpi_data.get('current', {}).items() if k != 'customer_breakdown'},
                'last_year_same_month': {k: v for k, v in kpi_data.get('last_year', {}).items() if k != 'customer_breakdown'},
                'last_month': kpi_data.get('last_month', {}),
                'yoy_change_pct': kpi_data.get('yoy', {}).get('change_pct'),
                'mom_change_pct': kpi_data.get('mom', {}).get('change_pct'),
                'target': kpi_calc.target,
                'gap': kpi_data.get('yoy', {}).get('gap', round(kpi_data.get('current', {}).get('per_customer', 0) - kpi_calc.target, 2)),
                'ytd': kpi_data.get('ytd', {}),
                'monthly_trend': kpi_data.get('trend', []),
                'customer_distribution': kpi_data.get('distribution', []),
                'non_compliant_customers': [{k: v for k, v in nc.items()} for nc in kpi_data.get('non_compliant', [])[:20]],
            } if kpi_data else {},
            'content': report_content
        }

        # 10. 保存文件
        self._save_report(result, year, month)

        return result

    def _generate_report_content(self, monthly_data: Dict, yoy_metrics: Dict,
                                  mom_metrics: Dict,
                                  last_year_data: Dict, last_month_data: Dict,
                                  year: int, month: int,
                                  top10_analysis: str = "", detailed_tables: str = "",
                                  api_key: str = "", provider: str = "gemini",
                                  model_name: str = "", base_url: str = "",
                                  labeled_issues: Dict = None,
                                  cross_week_analysis: str = "",
                                  next_month_plan: str = "",
                                  kpi_data: Dict = None) -> str:
        """生成Markdown格式的报告内容"""
        meta = monthly_data.get('meta', {})
        charts = monthly_data.get('charts', {})

        content = f"# {year}年{month}月 月总结分析报告\n\n"
        content += f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        content += f"**数据源**: {meta.get('source_file', '周报聚合(' + str(meta.get('week_count', 0)) + '周)')}\n\n"

        # 1. 工单总数概览（含同比和环比）
        content += "## 1. 工单总数概览\n\n"

        total = meta.get('total_tickets', 0)
        process = meta.get('count_process', 0)
        transferred = meta.get('count_transferred', 0)
        ratio = meta.get('ratio_transferred', 0)

        content += f"- **接收工单总数**: {total}"
        yoy_info = []
        mom_info = []
        if yoy_metrics.get('total_tickets'):
            yoy = yoy_metrics['total_tickets']
            yoy_info.append(f"同比{yoy['arrow']}{yoy['percent']}")
        if mom_metrics.get('total_tickets'):
            mom = mom_metrics['total_tickets']
            mom_info.append(f"环比{mom['arrow']}{mom['percent']}")
        if yoy_info or mom_info:
            content += f" ({' | '.join(yoy_info + mom_info)})"
        content += "\n"

        content += f"- **流程工单数 (核心)**: {process}"
        yoy_info = []
        mom_info = []
        if yoy_metrics.get('count_process'):
            yoy = yoy_metrics['count_process']
            yoy_info.append(f"同比{yoy['arrow']}{yoy['percent']}")
        if mom_metrics.get('count_process'):
            mom = mom_metrics['count_process']
            mom_info.append(f"环比{mom['arrow']}{mom['percent']}")
        if yoy_info or mom_info:
            content += f" ({' | '.join(yoy_info + mom_info)})"
        content += "\n"

        content += f"- **转出工单数**: {transferred}"
        if yoy_metrics.get('count_transferred'):
            yoy = yoy_metrics['count_transferred']
            content += f" ({yoy['arrow']}{yoy['percent']} 同比)"
        content += "\n"

        content += f"- **转出占比**: {ratio:.1f}%"
        if yoy_metrics.get('ratio_transferred'):
            yoy = yoy_metrics['ratio_transferred']
            content += f" ({yoy['arrow']}{yoy['percent']} 同比)"
        content += "\n\n"

        # 2. KPI达标分析
        if kpi_data:
            kpi_calc_for_md = KPICalculator()
            content += kpi_calc_for_md.generate_kpi_section_md(kpi_data, report_type="monthly")

        # 3. 工单占比分析
        content += "## 3. 工单占比分析\n\n"

        # 判断是否有同比/环比数据
        has_yoy_type = yoy_metrics and yoy_metrics.get('type_comparison')
        has_yoy_role = yoy_metrics and yoy_metrics.get('role_comparison')
        has_mom_type = mom_metrics and mom_metrics.get('type_comparison')

        # 构建环比数据字典
        mom_type_dict = {}
        if has_mom_type:
            for t in mom_metrics.get('type_comparison', []):
                mom_type_dict[t['type']] = t

        # 问题类型占比
        content += "### 问题类型占比TOP10\n\n"
        if has_yoy_type:
            content += "| 类型 | 本月占比 | 去年同月 | 同比变化 | 上月占比 | 环比变化 |\n"
            content += "|------|---------|---------|---------|---------|---------|\n"
            type_comp = yoy_metrics.get('type_comparison', [])
            for t in type_comp[:10]:
                yoy_arrow = t.get('arrow', '→')
                yoy_percent = t.get('percent', 'N/A')
                # 环比数据
                type_name = t['type']
                if type_name in mom_type_dict:
                    mom_t = mom_type_dict[type_name]
                    mom_val = mom_t.get('last_month', 0)
                    mom_arrow = mom_t.get('arrow', '→')
                    mom_percent = mom_t.get('percent', 'N/A')
                    content += f"| {type_name} | {t['current']:.1f}% | {t['last_year']:.1f}% | {yoy_arrow}{yoy_percent} | {mom_val:.1f}% | {mom_arrow}{mom_percent} |\n"
                else:
                    content += f"| {type_name} | {t['current']:.1f}% | {t['last_year']:.1f}% | {yoy_arrow}{yoy_percent} | - | - |\n"
        else:
            content += "| 类型 | 本月占比 |\n"
            content += "|------|---------|\n"
            type_counts = charts.get('type_counts', {})
            for type_name, pct in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
                content += f"| {type_name} | {pct:.1f}% |\n"

        # 处理角色占比
        content += "\n### 处理角色占比TOP5\n\n"
        if has_yoy_role:
            content += "| 角色 | 本月占比 | 去年同月 | 变化 |\n"
            content += "|------|---------|---------|------|\n"
            role_comp = yoy_metrics.get('role_comparison', [])
            for r in role_comp[:5]:
                arrow = r.get('arrow', '→')
                percent = r.get('percent', 'N/A')
                content += f"| {r['role']} | {r['current']:.1f}% | {r['last_year']:.1f}% | {arrow}{percent} |\n"
        else:
            content += "| 角色 | 本月占比 |\n"
            content += "|------|---------|\n"
            role_counts = charts.get('role_counts', {})
            for role_name, pct in sorted(role_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                content += f"| {role_name} | {pct:.1f}% |\n"

        # 4. TOP10 问题聚类
        content += "\n## 4. 工单重点关注 (TOP 10 聚类)\n\n"
        if top10_analysis:
            content += top10_analysis + "\n\n"
        else:
            content += "*(未配置API Key，无法生成AI聚类分析)*\n\n"

        # 获取工单明细（用于生成详细分析）
        ticket_details = monthly_data.get('ticket_details', {})

        # 5. 需求分析专题
        content += "## 5. 需求类工单分析\n\n"
        req_counts = charts.get('req_counts', {})
        if req_counts:
            content += "### 需求类型分布\n\n"
            content += "| 需求类型 | 占比 |\n"
            content += "|----------|------|\n"
            for req_type, pct in sorted(req_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                content += f"| {req_type} | {pct:.1f}% |\n"
            content += "\n"

        # 需求类详细分析（使用明细数据）
        req_tickets = ticket_details.get('requirement_tickets', [])
        req_ai_prompt = """基于以下需求类工单，请按照主题进行分类总结：

主题结构参考：
- 工作流产品结构（流程引擎、字段权限、工作流设计器等）
- 工作流上游业务（UI模板、业务对象等）
- 工作流平行业务（消息模板、业务活动等）
- 工作流下游业务（单据、消息中心等）

请分析各主题下的需求分布、纳入/计划解决的需求、拒绝的需求及理由。"""
        content += self._generate_category_analysis(
            req_tickets, "需求", req_ai_prompt,
            api_key, provider, model_name, base_url
        )

        # 5.2 "流程-"标签重点关注问题
        if labeled_issues and labeled_issues.get('process_labeled'):
            process_labeled = labeled_issues.get('process_labeled', [])
            if process_labeled:
                content += "### \"流程-\"标签重点关注问题\n\n"
                content += f"*本月共有 {len(process_labeled)} 个带\"流程-\"标签的工单*\n\n"
                content += "| 工单编号 | 问题描述 | 问题类型 | 标签 |\n"
                content += "|---------|---------|---------|------|\n"

                for ticket in process_labeled[:20]:  # 限制显示前20个
                    key = ticket.get('问题关键字', 'N/A')
                    summary = str(ticket.get('概要', ''))[:50].replace('\n', ' ').replace('|', '\\|')
                    # 兼容两种字段名
                    issue_type = ticket.get('自定义字段(研发确认问题类型)', '-') or ticket.get('研发确认问题类型', '-')
                    label = ticket.get('matched_label', '-') or ticket.get('标签', '-')

                    content += f"| {key} | {summary} | {issue_type} | {label} |\n"

                if len(process_labeled) > 20:
                    content += f"\n*...共 {len(process_labeled)} 个工单，仅显示前 20 个*\n"
                content += "\n"

        # 6. 操作类工单分析
        content += "## 6. 操作类工单分析\n\n"
        op_counts = charts.get('op_counts', {})
        if op_counts:
            content += "### 主要处理人分布\n\n"
            content += "| 处理人 | 占比 |\n"
            content += "|--------|------|\n"
            for name, pct in sorted(op_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                content += f"| {name} | {pct:.1f}% |\n"
            content += "\n"

        # 操作类详细分析
        op_tickets = ticket_details.get('operation_tickets', [])
        op_ai_prompt = """基于以下操作/咨询类工单，请按照主题分类总结：

请分析：
1. 各主题下的操作问题分布
2. 用户操作错误的问题有哪些
3. 用户不明白某个设置的问题有哪些
4. 产品改进建议"""
        content += self._generate_category_analysis(
            op_tickets, "操作", op_ai_prompt,
            api_key, provider, model_name, base_url
        )

        # 7. 转出类工单分析
        content += "## 7. 转出类工单分析\n\n"
        trans_counts = charts.get('trans_counts', {})
        if trans_counts:
            content += "### 转出项目分布\n\n"
            content += "| 项目名称 | 占比 |\n"
            content += "|----------|------|\n"
            for proj, pct in sorted(trans_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                content += f"| {proj} | {pct:.1f}% |\n"
            content += "\n"

        # 转出类详细分析
        trans_tickets = ticket_details.get('transferred_tickets', [])
        trans_ai_prompt = """基于以下转出工单，请分析转出情况：

请分析：
1. 主要转出问题总结
2. 按项目名称的工单问题分布
3. 主要转出项目是哪些
4. 这些项目的问题聚焦在哪里"""
        content += self._generate_category_analysis(
            trans_tickets, "转出", trans_ai_prompt,
            api_key, provider, model_name, base_url
        )

        # 8. 实施类工单分析
        content += "## 8. 实施类工单分析\n\n"
        impl_tickets = ticket_details.get('implementation_tickets', [])
        impl_ai_prompt = """基于以下实施类工单，请按主题分类进行总结：

请分析：
1. 实施类问题按主题分布
2. 用户实施的主要问题有哪些
3. 常见问题归纳
4. 改进建议"""
        content += self._generate_category_analysis(
            impl_tickets, "实施", impl_ai_prompt,
            api_key, provider, model_name, base_url
        )

        # 9. 运维类工单分析
        content += "## 9. 运维类工单分析\n\n"
        ops_tickets = ticket_details.get('ops_tickets', [])
        ops_ai_prompt = """基于以下运维类工单，请按主题分类进行总结：

请分析：
1. 运维类问题按主题分布
2. 用户遭遇的运维问题有哪些
3. 常见问题归纳
4. 改进建议"""
        content += self._generate_category_analysis(
            ops_tickets, "运维", ops_ai_prompt,
            api_key, provider, model_name, base_url
        )

        # 10. 跨周持续性分析（月报特有）
        if cross_week_analysis:
            content += "\n---\n\n"
            content += "## 10. 跨周持续性分析\n\n"
            content += "> 识别本月多周内持续出现的问题，持续未改善的问题需要升级处理优先级。\n\n"
            content += cross_week_analysis + "\n\n"

        # 11. 下月行动计划（月报核心价值）
        if next_month_plan:
            content += "\n---\n\n"
            content += next_month_plan + "\n\n"

        # 12. 详细清单（供查阅）
        if detailed_tables:
            content += "\n---\n\n"
            content += "# 详细工单清单\n\n"
            content += detailed_tables

        return content

    def _generate_ai_yoy_insights(self, current_data: Dict, last_year_data: Dict,
                                   api_key: str, provider: str, model_name: str, base_url: str) -> str:
        """使用AI生成同比洞察"""
        if not api_key:
            return ""

        # 构建提示
        current_meta = current_data.get('meta', {})
        last_meta = last_year_data.get('meta', {})

        current_charts = current_data.get('charts', {})
        last_charts = last_year_data.get('charts', {})

        prompt = f"""你是一名资深的工单分析专家。请根据以下两年同月份的工单数据对比，生成专业的同比分析报告。

【今年数据】({current_meta.get('period', '本月')})
- 工单总数: {current_meta.get('total_tickets', 0)}
- 流程工单: {current_meta.get('count_process', 0)}
- 转出工单: {current_meta.get('count_transferred', 0)}
- 问题类型分布: {dict(list(current_charts.get('type_counts', {}).items())[:5])}
- 处理角色分布: {dict(list(current_charts.get('role_counts', {}).items())[:3])}

【去年同月数据】({last_meta.get('period', '去年同期')})
- 工单总数: {last_meta.get('total_tickets', 0)}
- 流程工单: {last_meta.get('count_process', 0)}
- 转出工单: {last_meta.get('count_transferred', 0)}
- 问题类型分布: {dict(list(last_charts.get('type_counts', {}).items())[:5])}
- 处理角色分布: {dict(list(last_charts.get('role_counts', {}).items())[:3])}

请从以下角度生成分析报告（使用中文）：
1. 整体趋势总结（工单量变化、工作负载变化）
2. 问题类型变化分析（哪些类型增加/减少，可能的原因）
3. 团队效率变化（如果有数据支撑）
4. 建议与改进方向（基于对比结果给出可执行的建议）

请用专业的分析师口吻，数据驱动的论述方式，生成一份简洁但有洞察力的报告（300-500字）。"""

        try:
            llm_service = LLMService()
            response = llm_service.call_llm(
                prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url
            )
            return response
        except Exception as e:
            return f"AI分析生成失败: {str(e)}"

    def _generate_top10_focus(self, df: pd.DataFrame, api_key: str,
                               provider: str, model_name: str, base_url: str) -> str:
        """生成TOP10问题聚类分析（使用AI）"""
        if not api_key or df.empty:
            return ""

        # 准备工单数据（限制数量避免超出token限制）
        type_col = '自定义字段(研发确认问题类型)'
        label_col = '标签' if '标签' in df.columns else None

        # 优先选择带标签的工单
        if label_col:
            labeled_df = df[df[label_col].notna() & (df[label_col] != '')]
        else:
            labeled_df = pd.DataFrame()

        # 格式化工单数据
        def format_tickets(tickets_df, limit=80):
            lines = []
            for _, row in tickets_df.head(limit).iterrows():
                key = row.get('问题关键字', 'N/A')
                summary = row.get('概要', '')[:80]
                issue_type = row.get(type_col, '未分类')
                label = row.get(label_col, '') if label_col else ''
                label_str = f" [{label}]" if label else ""
                lines.append(f"- [{key}]{label_str} {summary} (类型: {issue_type})")
            return "\n".join(lines)

        prompt = f"""你是一名工单分析专家，目标是帮助团队**降低工单数量**。请根据以下工单列表，进行自动聚类分析，识别本月最值得关注的TOP 10个主要问题集群。

## 分析要求

**一、聚类分析表格**（必须输出）
输出格式为Markdown表格：
| 排名 | 问题集群名称 | 工单数 | 行动类型 | 责任方 | 预计降量/月 |

其中**行动类型**必须从以下四类中选一类：
- **A-用户培训**：用户操作错误或不理解功能，通过培训/文档/FAQ可消灭
- **B-顾问赋能**：技术顾问缺乏标准解法，通过知识库沉淀/方案赋能可消灭
- **C-诊断工具**：需要后台操作才能解决，需要开发自助诊断工具
- **D-产品规划**：功能缺失或设计不合理，需要产品规划并研发实现

**二、各聚类深度分析**
对每个集群给出：
- 问题描述和典型工单
- 行动类型选择理由
- 具体可执行的下一步行动（1句话，要具体）

**三、月度行动摘要**（必须输出，放最后）
输出"本月TOP行动建议"：
| 优先级 | 行动描述（具体） | 责任方 | 预计月减少工单 |
只列P1-P3，最多10条，每条要可执行（写清楚做什么、产出什么）。

带标签的重点工单（必须分析）：
{format_tickets(labeled_df, limit=20)}

全部工单列表：
{format_tickets(df, limit=80)}

请以Markdown格式输出分析结果。"""

        try:
            llm_service = LLMService()
            response = llm_service.call_llm(
                prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url
            )
            return response
        except Exception as e:
            return f"*(TOP10聚类分析失败: {str(e)})*"

    def _generate_top10_focus_from_tickets(self, tickets: List[Dict], api_key: str,
                                            provider: str, model_name: str, base_url: str) -> str:
        """生成TOP10问题聚类分析（周报聚合模式 - 使用ticket字典列表）"""
        if not api_key or not tickets:
            return ""

        # 准备工单数据（限制数量避免超出token限制）
        def format_tickets(ticket_list, limit=80):
            lines = []
            for t in ticket_list[:limit]:
                key = t.get('问题关键字', 'N/A')
                summary = t.get('概要', '')[:80]
                issue_type = t.get('研发确认问题类型', '未分类')
                lines.append(f"- [{key}] {summary} (类型: {issue_type})")
            return "\n".join(lines)

        prompt = f"""你是一名工单分析专家，目标是帮助团队**降低工单数量**。请根据以下工单列表，进行自动聚类分析，识别本月最值得关注的TOP 10个主要问题集群。

## 分析要求

**一、聚类分析表格**（必须输出）
输出格式为Markdown表格：
| 排名 | 问题集群名称 | 工单数 | 行动类型 | 责任方 | 预计降量/月 |

其中**行动类型**必须从以下四类中选一类：
- **A-用户培训**：用户操作错误或不理解功能，通过培训/文档/FAQ可消灭
- **B-顾问赋能**：技术顾问缺乏标准解法，通过知识库沉淀/方案赋能可消灭
- **C-诊断工具**：需要后台操作才能解决，需要开发自助诊断工具
- **D-产品规划**：功能缺失或设计不合理，需要产品规划并研发实现

**二、各聚类深度分析**
对每个集群给出：
- 问题描述和典型工单
- 行动类型选择理由
- 具体可执行的下一步行动（1句话，要具体）

**三、月度行动摘要**（必须输出，放最后）
输出"本月TOP行动建议"：
| 优先级 | 行动描述（具体） | 责任方 | 预计月减少工单 |
只列P1-P3，最多10条，每条要可执行（写清楚做什么、产出什么）。

全部工单列表：
{format_tickets(tickets, limit=80)}

请以Markdown格式输出分析结果。"""

        try:
            llm_service = LLMService()
            response = llm_service.call_llm(
                prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url
            )
            return response
        except Exception as e:
            return f"*(TOP10聚类分析失败: {str(e)})*"

    def _analyze_cross_week_patterns(self, year: int, month: int,
                                      api_key: str, provider: str, model_name: str, base_url: str) -> str:
        """
        跨周持续性分析：识别本月多周内持续出现的问题集群。
        返回Markdown格式的分析结果。
        """
        weekly_reports = self.monthly_analyzer._get_weekly_reports_for_month(year, month)
        if len(weekly_reports) < 2:
            return ""  # 少于2周数据，无法做跨周对比

        # 提取每周TOP10聚类内容（从report content中取）
        week_summaries = []
        for i, r in enumerate(weekly_reports):
            content = r.get('content', '')
            # 提取TOP10聚类部分（取第4节前200字符作为摘要）
            top10_match = re.search(r'## 4\..+?(?=## 5\.|\Z)', content, re.DOTALL)
            top10_text = top10_match.group(0)[:1500] if top10_match else content[:800]
            week_summaries.append(f"【第{i+1}周({r.get('_date','')[:10]})】\n{top10_text}")

        if not week_summaries:
            return ""

        summaries_text = "\n\n---\n\n".join(week_summaries)

        prompt = f"""你是一名工单分析专家。以下是{year}年{month}月共{len(weekly_reports)}周的TOP10问题聚类摘要。

请分析：
1. **持续出现的高风险问题**：在2周及以上都出现的问题集群，标注出现周次（如"连续3周TOP1"）
2. **本月新增问题**：只在后期周次出现、前期没有的问题
3. **已改善问题**：前期出现但后期消失的问题
4. **行动优先级判断**：持续出现未改善的问题，说明上周行动建议没有落地，需要升级处理

**输出格式**（Markdown表格）：

### 跨周持续问题（优先处理）
| 问题集群 | 出现周次 | 是否改善 | 紧急程度 | 建议升级行动 |

### 月度规律总结
（2-3句话说明本月工单的整体规律）

各周摘要内容如下：
{summaries_text}
"""

        try:
            llm_service = LLMService()
            response = llm_service.call_llm(
                prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url
            )
            return response
        except Exception as e:
            return f"*(跨周分析失败: {str(e)})*"

    def _generate_next_month_action_plan(self, year: int, month: int, top10_analysis: str,
                                          cross_week_analysis: str,
                                          api_key: str, provider: str, model_name: str, base_url: str) -> str:
        """
        生成下月行动计划：基于本月分析，给出下月具体可执行的行动项。
        """
        if not api_key:
            return ""

        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1

        prompt = f"""你是一名工单分析专家。根据{year}年{month}月的工单分析结果，请生成{next_year}年{next_month}月的行动计划。

本月TOP10聚类分析摘要：
{top10_analysis[:2000]}

本月跨周持续问题分析：
{cross_week_analysis[:1000]}

请输出**下月行动计划**，格式如下：

## {next_year}年{next_month}月行动计划

### 目标
- 工单降量目标：（基于分析估算可减少的工单数/占比）

### A类行动：用户培训与宣传（本月完成）
| 序号 | 具体行动 | 负责人建议 | 完成标准 |
（列举2-4条，每条具体可执行）

### B类行动：顾问知识库沉淀（本月完成）
| 序号 | 具体行动 | 负责人建议 | 完成标准 |
（列举2-3条）

### C类行动：诊断工具开发（排期）
| 序号 | 工具名称 | 解决的问题 | 开发优先级 |
（列举1-3条，按降量收益排序）

### D类行动：产品规划输入（本月提需求）
| 序号 | 需求描述 | 已持续周数 | 涉及客户数估计 |
（列举1-3条，持续出现且多客户的优先）

请确保每条行动都具体可执行，避免模糊表述。"""

        try:
            llm_service = LLMService()
            response = llm_service.call_llm(
                prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url
            )
            return response
        except Exception as e:
            return f"*(行动计划生成失败: {str(e)})*"

    def _generate_detailed_tables(self, df: pd.DataFrame, api_key: str,
                                   provider: str, model_name: str, base_url: str) -> str:
        """生成各类工单的详细清单表格"""
        if df.empty:
            return ""

        type_col = '自定义字段(研发确认问题类型)'
        assignee_col = '经办人'

        content = ""

        # Helper: 生成Markdown表格
        def generate_md_table(tkts_df, columns):
            # 检查是否是DataFrame（支持list传入）
            if isinstance(tkts_df, list):
                if not tkts_df:
                    return "*(无此类工单)*\n"
                # 转换为DataFrame
                tkts_df = pd.DataFrame(tkts_df)
            elif hasattr(tkts_df, 'empty') and tkts_df.empty:
                return "*(无此类工单)*\n"

            header = "| " + " | ".join(columns) + " |\n"
            separator = "| " + " | ".join(["---"] * len(columns)) + " |\n"

            body = ""
            for _, row in tkts_df.iterrows():
                row_str = "|"
                for col in columns:
                    val = str(row.get(col, '')).replace('\n', ' ').replace('|', '\\|')
                    # 字段映射
                    if col == '问题编号':
                        val = str(row.get('问题关键字', ''))
                    elif col == '问题描述':
                        val = str(row.get('概要', ''))[:80]
                    elif col == '问题类型':
                        val = str(row.get(type_col, ''))
                    elif col == '研发确认问题类型':
                        val = str(row.get(type_col, ''))
                    elif col == '经办人':
                        val = self.monthly_analyzer.get_realname(str(row.get(assignee_col, '')))
                    elif col == '创建时间':
                        val = str(row.get('创建日期', ''))[:10]
                    elif col == '完成时间':
                        val = str(row.get('解决日期', ''))[:10]
                    elif col == '问题用时':
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

        # Helper: AI主题总结
        def generate_ai_summary(tkts_df, summary_prompt, default_msg=""):
            # 检查是否是DataFrame或list
            if isinstance(tkts_df, list):
                if not tkts_df or not api_key:
                    return default_msg
                # 转换为DataFrame
                tkts_df = pd.DataFrame(tkts_df)
            elif hasattr(tkts_df, 'empty') and tkts_df.empty or not api_key:
                return default_msg

            # 准备工单数据
            tickets_text = "\n".join([
                f"- [{row.get('问题关键字', 'N/A')}] {row.get('概要', '')[:100]}"
                for _, row in tkts_df.head(50).iterrows()
            ])

            prompt = f"""{summary_prompt}

工单列表：
{tickets_text}

请用简洁的中文总结（200字以内）。"""

            try:
                llm_service = LLMService()
                return llm_service.call_llm(
                    prompt,
                    api_key=api_key,
                    provider=provider,
                    model_name=model_name,
                    base_url=base_url
                )
            except:
                return default_msg

        # 1. 需求类工单
        req_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('需求') >= 0 or
            '需求' in str(r.get('自定义字段(解决方案)', '')) or
            '不支持' in str(r.get('自定义字段(解决方案)', '')), axis=1)]

        content += "## 需求类工单详细清单\n\n"
        req_summary = generate_ai_summary(
            req_df,
            "基于以下需求类工单，请按照主题进行分类总结：\n\n主题结构参考：\n- 工作流产品结构（流程引擎、字段权限、工作流设计器等）\n- 工作流上游业务（UI模板、业务对象等）\n- 工作流平行业务（消息模板、业务活动等）\n- 工作流下游业务（单据、消息中心等）\n\n请分析各主题下的需求分布、纳入/计划解决的需求、拒绝的需求及理由。",
            ""
        )
        if req_summary:
            content += "### AI主题总结\n" + req_summary + "\n\n"
        content += "### 详细清单\n"
        content += generate_md_table(req_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时'])
        content += "\n"

        # 2. 操作类工单
        op_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('应用操作') >= 0 or
            '操作' in str(r.get('自定义字段(解决方案)', '')) or
            '设置' in str(r.get('自定义字段(解决方案)', '')), axis=1)]

        content += "## 操作类工单详细清单\n\n"
        op_summary = generate_ai_summary(
            op_df,
            "基于以下操作/咨询类工单，请按照主题分类总结：\n\n请分析：\n1. 各主题下的操作问题分布\n2. 用户操作错误的问题有哪些\n3. 用户不明白某个设置的问题有哪些\n4. 产品改进建议",
            ""
        )
        if op_summary:
            content += "### AI主题总结\n" + op_summary + "\n\n"
        content += "### 详细清单\n"
        content += generate_md_table(op_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时'])
        content += "\n"

        # 3. 转出类工单
        transfer_df = df[df['Project'] != '云平台-流程中心']

        content += "## 转出类工单详细清单\n\n"
        trans_summary = generate_ai_summary(
            transfer_df,
            "基于以下转出工单，请分析转出情况：\n\n请分析：\n1. 主要转出问题总结\n2. 按项目名称的工单问题分布\n3. 主要转出项目是哪些\n4. 这些项目的问题聚焦在哪里",
            ""
        )
        if trans_summary:
            content += "### AI主题总结\n" + trans_summary + "\n\n"
        content += "### 详细清单\n"
        content += generate_md_table(transfer_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时'])
        content += "\n"

        # 4. 实施类工单
        impl_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('实施') >= 0 or
            '实施' in str(r.get('自定义字段(解决方案)', '')), axis=1)]

        content += "## 实施类工单详细清单\n\n"
        impl_summary = generate_ai_summary(
            impl_df,
            "基于以下实施类工单，请按主题分类进行总结：\n\n请分析：\n1. 实施类问题按主题分布\n2. 用户实施的主要问题有哪些\n3. 常见问题归纳\n4. 改进建议",
            ""
        )
        if impl_summary:
            content += "### AI主题总结\n" + impl_summary + "\n\n"
        content += "### 详细清单\n"
        content += generate_md_table(impl_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时'])
        content += "\n"

        # 5. 运维类工单
        ops_df = df[df.apply(lambda r:
            str(r.get(type_col, '')).find('运维') >= 0 or
            '运维' in str(r.get('自定义字段(解决方案)', '')), axis=1)]

        content += "## 运维类工单详细清单\n\n"
        ops_summary = generate_ai_summary(
            ops_df,
            "基于以下运维类工单，请按主题分类进行总结：\n\n请分析：\n1. 运维类问题按主题分布\n2. 用户遭遇的运维问题有哪些\n3. 常见问题归纳\n4. 改进建议",
            ""
        )
        if ops_summary:
            content += "### AI主题总结\n" + ops_summary + "\n\n"
        content += "### 详细清单\n"
        content += generate_md_table(ops_df, ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时'])
        content += "\n"

        return content

    def _generate_category_analysis(self, tickets: List[Dict], category_name: str,
                                    ai_prompt: str, api_key: str, provider: str,
                                    model_name: str, base_url: str) -> str:
        """
        为特定类别的工单生成AI主题总结和详细清单
        用于周报聚合模式（从字典列表生成）
        """
        if not tickets:
            return f"*(本月无{category_name}类工单)*\n"

        content = ""

        # AI主题总结
        if api_key and tickets:
            tickets_text = "\n".join([
                f"- [{t.get('问题关键字', 'N/A')}] {t.get('概要', '')[:100]}"
                for t in tickets[:50]
            ])

            prompt = f"""{ai_prompt}

工单列表：
{tickets_text}

请用简洁的中文总结（200字以内）。"""

            try:
                llm_service = LLMService()
                ai_summary = llm_service.call_llm(
                    prompt,
                    api_key=api_key,
                    provider=provider,
                    model_name=model_name,
                    base_url=base_url
                )
                if ai_summary:
                    content += "### AI主题总结\n" + ai_summary + "\n\n"
            except Exception as e:
                print(f"AI分析生成失败 ({category_name}): {e}")

        # 生成详细清单表格
        content += "### 详细清单\n"
        content += self._generate_ticket_table_from_list(tickets)
        content += "\n"

        return content

    def _generate_ticket_table_from_list(self, tickets: List[Dict]) -> str:
        """从工单字典列表生成Markdown表格"""
        if not tickets:
            return "*(无此类工单)*\n"

        columns = ['问题类型', '问题编号', '问题描述', '经办人', '研发确认问题类型', '创建时间', '完成时间', '问题用时']

        header = "| " + " | ".join(columns) + " |\n"
        separator = "| " + " | ".join(["---"] * len(columns)) + " |\n"

        body = ""
        for t in tickets:
            row_str = "|"
            for col in columns:
                # 字段映射
                if col == '问题类型':
                    val = str(t.get('研发确认问题类型', ''))
                elif col == '问题编号':
                    val = str(t.get('问题关键字', ''))
                elif col == '问题描述':
                    val = str(t.get('概要', ''))[:80].replace('\n', ' ').replace('|', '\\|')
                elif col == '经办人':
                    val = str(t.get('经办人', ''))
                elif col == '研发确认问题类型':
                    val = str(t.get('研发确认问题类型', ''))
                elif col == '创建时间':
                    val = str(t.get('创建日期', ''))[:10]
                elif col == '完成时间':
                    val = str(t.get('解决日期', ''))[:10] if t.get('解决日期') else '-'
                elif col == '问题用时':
                    val = str(t.get('问题用时', '-'))
                else:
                    val = str(t.get(col, '')).replace('\n', ' ').replace('|', '\\|')

                row_str += f" {val} |"
            body += row_str + "\n"

        return header + separator + body

    def _save_report(self, result: Dict, year: int, month: int):
        """保存月报到文件"""
        # 从元数据中提取数据起止日期
        meta = result.get('meta', {})
        data_start_date = meta.get('data_start_date', f"{year}-{month:02d}-01")
        data_end_date = meta.get('data_end_date', f"{year}-{month:02d}-28")  # 默认值，会被覆盖

        # 计算月末日期（如果未提供）
        if 'data_end_date' not in meta:
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            data_end_date = f"{year}-{month:02d}-{last_day}"

        filename_base = f"Monthly_Report_{year}{month:02d}_{data_start_date}_{data_end_date}"

        # 保存JSON
        json_path = os.path.join(self.monthly_report_dir, f"{filename_base}.json")
        with open(json_path, 'w', encoding='utf-8') as f:
            # 移除原始DataFrame（如果有）
            save_data = result.copy()
            if 'raw_df' in save_data.get('meta', {}):
                del save_data['meta']['raw_df']
            json.dump(save_data, f, ensure_ascii=False, indent=2)

        print(f"月报JSON已保存: {json_path}")

        # 保存Markdown
        md_path = os.path.join(self.monthly_report_dir, f"{filename_base}.md")
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(result.get('content', ''))

        print(f"月报Markdown已保存: {md_path}")


def list_monthly_reports() -> List[Dict]:
    """列出所有可用的月报"""
    if not os.path.exists(MONTHLY_REPORT_DIR):
        return []

    reports = []
    for f in os.listdir(MONTHLY_REPORT_DIR):
        if f.startswith('_'):
            continue
        if f.endswith('.json'):
            # 新格式: Monthly_Report_YYYYMM_YYYY-MM-DD_YYYY-MM-DD.json
            # 尝试提取年月和数据起止日期
            match = re.search(r'Monthly_Report_(\d{6})_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})', f)
            if match:
                yyyymm = match.group(1)
                year = int(yyyymm[:4])
                month = int(yyyymm[4:6])
                data_start_date = match.group(2)
                data_end_date = match.group(3)

                # 尝试读取文件获取更完整的元数据
                filepath = os.path.join(MONTHLY_REPORT_DIR, f)
                try:
                    with open(filepath, 'r', encoding='utf-8') as file:
                        data = json.load(file)
                        meta = data.get('meta', {})
                        reports.append({
                            'filename': f,
                            'year': year,
                            'month': month,
                            'period': f"{year}年{month}月",
                            'data_start_date': meta.get('data_start_date', data_start_date),
                            'data_end_date': meta.get('data_end_date', data_end_date),
                            'data_period': f"{data_start_date} 至 {data_end_date}",
                            'generated_at': meta.get('generated_at', ''),
                            'total_tickets': meta.get('total_tickets', 0)
                        })
                except:
                    # 读取失败，使用文件名解析的数据
                    reports.append({
                        'filename': f,
                        'year': year,
                        'month': month,
                        'period': f"{year}年{month}月",
                        'data_start_date': data_start_date,
                        'data_end_date': data_end_date,
                        'data_period': f"{data_start_date} 至 {data_end_date}",
                        'generated_at': '',
                        'total_tickets': 0
                    })
            else:
                # 尝试旧格式: Monthly_Report_YYYYMM.json
                match = re.search(r'Monthly_Report_(\d{6})', f)
                if match:
                    yyyymm = match.group(1)
                    year = int(yyyymm[:4])
                    month = int(yyyymm[4:6])

                    # 尝试读取文件获取元数据
                    filepath = os.path.join(MONTHLY_REPORT_DIR, f)
                    data_start_date = f"{year}-{month:02d}-01"
                    import calendar
                    last_day = calendar.monthrange(year, month)[1]
                    data_end_date = f"{year}-{month:02d}-{last_day}"

                    try:
                        with open(filepath, 'r', encoding='utf-8') as file:
                            data = json.load(file)
                            meta = data.get('meta', {})
                            data_start_date = meta.get('data_start_date', data_start_date)
                            data_end_date = meta.get('data_end_date', data_end_date)
                            reports.append({
                                'filename': f,
                                'year': year,
                                'month': month,
                                'period': f"{year}年{month}月",
                                'data_start_date': data_start_date,
                                'data_end_date': data_end_date,
                                'data_period': f"{data_start_date} 至 {data_end_date}",
                                'generated_at': meta.get('generated_at', ''),
                                'total_tickets': meta.get('total_tickets', 0)
                            })
                    except:
                        reports.append({
                            'filename': f,
                            'year': year,
                            'month': month,
                            'period': f"{year}年{month}月",
                            'data_start_date': data_start_date,
                            'data_end_date': data_end_date,
                            'data_period': f"{data_start_date} 至 {data_end_date}",
                            'generated_at': '',
                            'total_tickets': 0
                        })

    # 按数据起始日期倒序
    reports.sort(key=lambda x: (x['year'], x['month']), reverse=True)
    return reports


def get_monthly_report(filename: str) -> Optional[Dict]:
    """获取指定月报的完整内容"""
    filepath = os.path.join(MONTHLY_REPORT_DIR, filename)

    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading monthly report {filename}: {e}")
        return None


if __name__ == "__main__":
    # 测试生成当前月份的月报
    now = datetime.now()
    generator = MonthlyReportGenerator()
    result = generator.generate(
        year=now.year,
        month=now.month
    )
    print(json.dumps(result['meta'], ensure_ascii=False, indent=2))
