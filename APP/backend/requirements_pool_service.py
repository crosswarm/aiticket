import os
import re
import pandas as pd
import json
import logging
import threading
import uuid
from typing import List, Dict, Optional, Any
from vector_store import VectorStore
from analysis import SRC_DIR as ANALYSIS_SRC_DIR
from agents.req_analyst_agent import ReqAnalystAgent
from datetime import datetime
from design_fact_service import DesignFactService

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 配置常量 - 可扩展配置
# 将相对路径转换为绝对路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（APP的父目录）
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
SRC_DIR = os.path.normpath(os.path.join(PROJECT_ROOT, "src"))
REPORT_DIR = os.path.normpath(os.path.join(PROJECT_ROOT, "conclusion/WeeklyReports"))
os.makedirs(REPORT_DIR, exist_ok=True)

# 周报解析配置
REPORT_CONFIG = {
    'section_patterns': [
        # 四级标题（需求文档标准格式）
        r'####\s*2\.?\s*纳入[/\\]?计划解决的需求.*?(?=\n#### |\n## |\Z)',
        r'####\s*纳入[/\\]?计划解决的需求.*?(?=\n#### |\n## |\Z)',
        # 二级标题
        r'##\s*纳入[/\\]?计划解决的需求.*?(?=\n## |\Z)',
        # 三级标题（实际周报格式：### 2. 纳入/计划解决的需求）
        r'###\s*\d+\.?\s*纳入[/\\]?计划解决的需求.*?(?=\n### |\n## |\n#### |$)',
        r'###\s*纳入[/\\]?计划解决的需求.*?(?=\n### |\n## |\n#### |$)',
        # 新增：匹配"## 5. 需求类工单分析"章节（用于从详细清单表格提取）
        r'##\s*\d+\.?\s*需求类工单分析.*?(?=\n## |\Z)',
        r'###\s*\d+\.?\s*需求类工单.*?(?=\n### |\n## |\Z)',
    ],
    # 支持两种工单ID格式：[MYPROJECT-59477] 和 **MYPROJECT-59477**
    'ticket_id_pattern': r'(?:\*\*\[|\*\*)([A-Z]{2,10}-\d{3,10})(?:\]|\*\*)',
    'csv_columns': {
        'issue_key': '问题关键字',
        'type': '自定义字段(研发确认问题类型)',
        'solution': '自定义字段(解决方案)',
        'reply_method': '自定义字段(回复方式)',
        'module': '自定义字段(领域模块)',
        'customer_type': '自定义字段(客户问题类型)',
        'summary': '概要',
        'description': '描述',
        'creator': '创建者',
        'assignee': '经办人',
        'created_date': '创建日期',
        'status': '状态',
        'labels': '标签',
        'project_desc': '项目描述',
    },
    # 识别为需求类工单的关键词（解决方案字段）- 扩展匹配范围
    'requirement_keywords': [
        '纳入需求库', '计划解决', '后续优化', '预计.*迭代',
        '待进一步收集需求规划', '待.*规划', '列入需求', '需求库',
        '统一规划实现', '排期.*版本', '预计.*版本',
        # 新增：更广泛的需求相关关键词
        '需求问题', '功能建议', '优化建议', '改进建议',
        '功能需求', '产品需求', '需求收集', '待评估',
        '后续版本', '下版本', '后续支持', '规划中',
    ],
    # 研发确认问题类型为需求的类型标识 - 扩展类型匹配
    'requirement_types': ['需求', '需求问题', '功能需求', '产品需求', '优化需求'],
}

# 需求池筛选配置
POOL_CONFIG = {
    'filter_column': '自定义字段(回复方式)',  # 筛选列（回复方式=纳入需求库）
    'filter_value': '纳入需求库',              # 筛选值
    'date_column': '创建日期',                 # 日期列（用于日期范围筛选）
    'project_column': '项目关键字',            # 项目列（用于项目过滤）
    'default_project': 'MYPROJECT',                # 默认只提取MYPROJECT项目，为空则不限
}

# 扩展状态定义（新增）
REQUIREMENT_STATUSES = {
    'new': '待分析',
    'analyzing': '分析中',
    'to_review': '待评审',
    'drafting': '初稿生成中',
    'draft_review': '初稿待审核',
    'draft_ready': '初稿待提交',
    'scheduled': '已排期',
    'developing': '开发中',      # 新增
    'pending_deploy': '待上线',   # 新增
    'deployed': '已上线',         # 新增
    'rejected': '已废弃'          # 改为废弃
}

# 预编译正则表达式
SECTION_PATTERNS = [re.compile(p, re.DOTALL) for p in REPORT_CONFIG['section_patterns']]
TICKET_ID_PATTERN = re.compile(REPORT_CONFIG['ticket_id_pattern'])

class RequirementsPoolService:
    def __init__(self, vector_store: VectorStore, kb_runtime_service: Any = None, design_fact_service: DesignFactService | None = None):
        self.vector_store = vector_store
        self.agent = ReqAnalystAgent(vector_store)
        self.kb_runtime_service = kb_runtime_service
        self.design_fact_service = design_fact_service
        self.analysis_tasks: Dict[str, Dict[str, Any]] = {}
        self.draft_service = None

    def _build_requirement_metadata(self, req: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = dict(req or {})
        payload.update(overrides or {})
        return {
            'status': payload.get('status', 'new'),
            'source_issues': payload.get('source_issues', []),
            'ai_analysis': payload.get('ai_analysis', {}),
            'review_records': payload.get('review_records', []),
            'entry_source': payload.get('entry_source', ''),
            'requirement_fact_packet': payload.get('requirement_fact_packet', {}),
            'created_at': payload.get('created_at', datetime.now().isoformat()),
            'feishu_notified': payload.get('feishu_notified', False),
        }

    def _normalize_manual_req_id(self, req_id: str) -> str:
        normalized = str(req_id or "").strip().upper()
        if not normalized or not re.fullmatch(r"[A-Z0-9_-]+", normalized):
            raise ValueError("Invalid requirement code")
        return normalized

    def create_manual_requirement(self, req_id: str, title: str, description: str) -> Dict[str, Any]:
        normalized_req_id = self._normalize_manual_req_id(req_id)
        clean_title = str(title or "").strip()
        clean_description = str(description or "").strip()

        if not clean_title:
            raise ValueError("Title is required")
        if not clean_description:
            raise ValueError("Description is required")

        existing = self.vector_store.get_requirement(normalized_req_id)
        if existing:
            raise ValueError("Requirement code already exists")

        created_at = datetime.now().isoformat()
        metadata = {
            'status': 'new',
            'source_issues': [],
            'ai_analysis': {},
            'review_records': [],
            'entry_source': 'manual_entry',
            'requirement_fact_packet': {},
            'created_at': created_at,
        }
        success = self.vector_store.upsert_requirement(normalized_req_id, clean_title, clean_description, metadata)
        if not success:
            raise RuntimeError("Failed to create requirement")

        return {
            'req_id': normalized_req_id,
            'title': clean_title,
            'description': clean_description,
            'status': 'new',
            'source_issues': [],
            'ai_analysis': {},
            'review_records': [],
            'entry_source': 'manual_entry',
            'requirement_fact_packet': {},
            'created_at': created_at,
            'updated_at': created_at,
        }

    def get_requirement_fact_packet(self, req_id: str) -> Dict[str, Any]:
        req = self.vector_store.get_requirement(req_id)
        if not req:
            raise ValueError("Requirement not found")
        if self.design_fact_service:
            return self.design_fact_service.normalize_fact_packet(req.get('requirement_fact_packet', {}))
        return req.get('requirement_fact_packet', {}) or {}

    def save_requirement_fact_packet(self, req_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = self.vector_store.get_requirement(req_id)
        if not req:
            raise ValueError("Requirement not found")
        old_req = req.get('requirement_fact_packet') or {}
        packet = self.design_fact_service.normalize_fact_packet(payload) if self.design_fact_service else dict(payload or {})
        req['requirement_fact_packet'] = packet
        success = self.vector_store.upsert_requirement(req_id, req['title'], req['description'], self._build_requirement_metadata(req))
        if not success:
            raise RuntimeError("Failed to save requirement fact packet")

        # 自动采集备注中的知识到 KB
        try:
            from kb_auto_import import get_auto_import
            _auto_import = get_auto_import()
            if _auto_import:
                old_notes = old_req.get('manual_notes', '') if old_req else ''
                new_notes = payload.get('manual_notes', '') if payload else ''
                if new_notes and new_notes != old_notes:
                    _auto_import.extract_and_save(
                        new_notes,
                        source_context={'type': 'req_note', 'ref_id': req_id or ''},
                    )
        except Exception:
            pass

        return packet

    def get_requirement_knowledge_context(self, req_id: str) -> Dict[str, Any]:
        req = self.vector_store.get_requirement(req_id)
        if not req:
            raise ValueError("Requirement not found")
        if not self.design_fact_service:
            return {
                "fact_packet": req.get("requirement_fact_packet", {}) or {},
                "design_fact_bundle": {
                    "design_principles": [],
                    "process_rules": [],
                    "step_rules": [],
                    "tenant_params": [],
                    "document_properties": [],
                    "coverage_summary": {"total_fact_count": 0, "missing_fact_count": 0},
                    "missing_facts": [],
                },
                "competitor_dossiers": [],
            }
        return self.design_fact_service.build_requirement_context(req, (req.get("ai_analysis") or {}).get("evidence_bundle", {}))

    def _normalize_ai_analysis(self, analysis_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        analysis = dict(analysis_result or {})
        root_cause = str(analysis.get('root_cause', '') or '').strip()
        module = str(analysis.get('module', '') or '').strip()
        mvp_suggestion = str(analysis.get('mvp_suggestion', '') or '').strip()

        parse_failure_markers = [
            '解析失败',
            'expecting value',
            'char 0',
            'jsondecodeerror',
        ]
        combined_text = f"{root_cause}\n{mvp_suggestion}".lower()

        if any(marker in combined_text for marker in parse_failure_markers):
            analysis['root_cause'] = '模型未返回有效的结构化分析结果，请重试。'
            analysis['module'] = '待确认'
            analysis['mvp_suggestion'] = '本次自动分析未形成可用结论，请检查模型配置或稍后重试。'
            return analysis

        if not root_cause and not mvp_suggestion:
            analysis['root_cause'] = '模型未返回有效的结构化分析结果，请重试。'
            analysis['module'] = module or '待确认'
            analysis['mvp_suggestion'] = '本次自动分析未形成可用结论，请检查模型配置或稍后重试。'
            return analysis

        if module == '未知' and ('api key' not in combined_text and '未配置' not in combined_text):
            analysis['module'] = '待确认'

        return analysis

    def _build_analysis_evidence_bundle(self, req: Dict, analysis_result: Dict, llm_config: Dict = None) -> Dict:
        if not self.kb_runtime_service:
            return {}

        summary_parts = [req.get('title', '').strip(), req.get('description', '').strip()[:1000]]
        summary = "\n".join(part for part in summary_parts if part).strip()
        module_hint = analysis_result.get('module', '')

        try:
            evidence_bundle = self.kb_runtime_service.analyze(
                summary=summary,
                module_hint=module_hint,
                top_k=8,
                llm_config=llm_config or {},
            )
        except Exception as exc:
            logger.warning(f"[ReqPool] build evidence bundle failed for {req.get('req_id')}: {exc}")
            return {}

        return evidence_bundle or {}

    def extract_from_src_csv(self, date_range: Dict = None) -> int:
        """从src目录CSV文件提取纳入需求库的工单

        Args:
            date_range: 日期范围筛选 {'start': '2026-01-01', 'end': '2026-02-28'}

        Returns:
            成功添加的需求数量
        """
        # 1. 获取所有符合条件的CSV文件
        csv_files = self._get_src_csv_files()

        total_added = 0
        total_skipped = 0
        total_failed = 0

        for csv_file in csv_files:
            try:
                df = pd.read_csv(csv_file)
                df.columns = [c.strip() for c in df.columns]

                # 2. 筛选"自定义字段(回复方式)" = "纳入需求库"
                filter_col = POOL_CONFIG['filter_column']
                filter_val = POOL_CONFIG['filter_value']

                if filter_col not in df.columns:
                    logger.warning(f"[ReqPool] CSV文件缺少{filter_col}列: {csv_file}")
                    continue

                # 精确匹配"纳入需求库"
                req_df = df[df[filter_col].astype(str).str.strip() == filter_val]

                # 项目过滤（默认只提取指定项目）
                default_project = POOL_CONFIG.get('default_project', '')
                project_col = POOL_CONFIG.get('project_column', '项目关键字')
                if default_project and project_col in req_df.columns:
                    before_count = len(req_df)
                    req_df = req_df[req_df[project_col].astype(str).str.strip() == default_project]
                    if before_count != len(req_df):
                        logger.info(f"[ReqPool] 项目过滤 {default_project}: {before_count} → {len(req_df)} 条")

                logger.info(f"[ReqPool] 文件 {os.path.basename(csv_file)}: 找到 {len(req_df)} 条纳入需求库的记录")

                # 3. 日期范围筛选（如果指定）
                date_col = REPORT_CONFIG['csv_columns'].get('created_date', '创建日期')
                if date_range and date_col in req_df.columns:
                    try:
                        req_df = req_df.copy()
                        req_df[date_col] = pd.to_datetime(req_df[date_col], errors='coerce')

                        if date_range.get('start'):
                            start_date = pd.to_datetime(date_range['start']).date()
                            req_df = req_df[req_df[date_col].dt.date >= start_date]
                        if date_range.get('end'):
                            end_date = pd.to_datetime(date_range['end']).date()
                            req_df = req_df[req_df[date_col].dt.date <= end_date]

                        logger.info(f"[ReqPool] 日期筛选后: 剩余 {len(req_df)} 条记录")
                    except Exception as e:
                        logger.warning(f"[ReqPool] 日期筛选失败: {e}")

                # 4. 保存到需求池
                for _, row in req_df.iterrows():
                    result = self._save_requirement_row(row)
                    if result == 'added':
                        total_added += 1
                    elif result == 'skipped':
                        total_skipped += 1
                    else:
                        total_failed += 1

            except Exception as e:
                logger.error(f"[ReqPool] 处理CSV文件失败 {csv_file}: {e}")
                continue

        logger.info(f"[ReqPool] 从src目录提取完成，添加: {total_added}, 跳过: {total_skipped}, 失败: {total_failed}")
        return total_added

    def _get_src_csv_files(self) -> List[str]:
        """获取src目录下的所有周数据CSV文件"""
        if not os.path.exists(SRC_DIR):
            logger.error(f"[ReqPool] src目录不存在: {SRC_DIR}")
            return []

        # 匹配周数据文件（排除年度汇总文件如"2025完成"）
        csv_files = []
        for f in os.listdir(SRC_DIR):
            if f.endswith('.csv') and '周数据' in f:
                csv_files.append(os.path.join(SRC_DIR, f))

        # 按文件名排序（时间倒序）
        csv_files.sort(reverse=True)
        logger.info(f"[ReqPool] 找到 {len(csv_files)} 个周数据CSV文件")
        return csv_files

    def _save_requirement_row(self, row: pd.Series) -> str:
        """保存单个CSV行到需求池，提取尽可能多的有效信息

        Returns:
            'added' - 新增成功
            'skipped' - 已存在跳过
            'failed' - 失败
        """
        try:
            logger.info("[ReqPool] _save_requirement_row V2 executing - enhanced version")
            cols = REPORT_CONFIG['csv_columns']

            # 基础字段
            issue_key = str(row.get(cols['issue_key'], '')).strip()
            if not issue_key:
                return 'failed'

            req_id = f"REQ-{issue_key}"

            # 检查是否已存在
            existing = self.vector_store.get_requirement(req_id)
            if existing and existing.get('status') != 'new':
                logger.debug(f"[ReqPool] Skipping {req_id}: existing status={existing.get('status')}")
                return 'skipped'

            # 提取各个字段
            summary = str(row.get(cols['summary'], ''))
            solution = str(row.get(cols['solution'], ''))
            reply_method = str(row.get(cols['reply_method'], ''))
            issue_type = str(row.get(cols['type'], ''))
            module = str(row.get(cols['module'], ''))
            customer_type = str(row.get(cols['customer_type'], ''))
            creator = str(row.get(cols['creator'], ''))
            assignee = str(row.get(cols['assignee'], ''))
            created_date = str(row.get(cols['created_date'], ''))
            status = str(row.get(cols['status'], ''))
            labels = str(row.get(cols['labels'], ''))
            project_desc = str(row.get(cols['project_desc'], ''))
            raw_desc = str(row.get(cols['description'], ''))

            # 构建完整描述（尽可能多地包含有价值信息）
            description_parts = []

            # 1. 原始工单信息
            description_parts.append("【工单基本信息】")
            description_parts.append(f"工单ID: {issue_key}")
            if creator and creator != 'nan':
                description_parts.append(f"创建者: {creator}")
            if assignee and assignee != 'nan':
                description_parts.append(f"经办人: {assignee}")
            if created_date and created_date != 'nan':
                description_parts.append(f"创建日期: {created_date}")
            if status and status != 'nan':
                description_parts.append(f"当前状态: {status}")

            # 2. 分类信息
            description_parts.append("\n【问题分类】")
            if issue_type and issue_type != 'nan':
                description_parts.append(f"研发确认问题类型: {issue_type}")
            if module and module != 'nan':
                description_parts.append(f"领域模块: {module}")
            if customer_type and customer_type != 'nan':
                description_parts.append(f"客户问题类型: {customer_type}")
            if labels and labels != 'nan':
                description_parts.append(f"标签: {labels}")

            # 3. 原始描述（如果存在且不为空）
            if raw_desc and raw_desc != 'nan' and raw_desc.strip():
                description_parts.append("\n【原始工单描述】")
                description_parts.append(raw_desc)

            # 4. 项目信息
            if project_desc and project_desc != 'nan' and project_desc.strip():
                description_parts.append("\n【项目描述】")
                description_parts.append(project_desc)

            # 5. 处理方案
            description_parts.append("\n【处理方案记录】")
            if solution and solution != 'nan':
                description_parts.append(solution)
            else:
                description_parts.append("暂无处理方案")

            # 6. 回复方式
            if reply_method and reply_method != 'nan':
                description_parts.append(f"\n【回复方式】{reply_method}")

            full_desc = "\n".join(description_parts)

            # 构建元数据
            metadata = {
                'status': 'new',
                'source_issues': [issue_key],
                'created_at': existing.get('created_at') if existing else datetime.now().isoformat(),
                'is_planned': True,
                # 新增：保存额外字段到metadata供后续使用
                'extra_fields': {
                    'issue_type': issue_type if issue_type != 'nan' else '',
                    'module': module if module != 'nan' else '',
                    'customer_type': customer_type if customer_type != 'nan' else '',
                    'creator': creator if creator != 'nan' else '',
                    'assignee': assignee if assignee != 'nan' else '',
                    'created_date': created_date if created_date != 'nan' else '',
                    'labels': labels if labels != 'nan' else '',
                }
            }

            success = self.vector_store.upsert_requirement(req_id, summary, full_desc, metadata)
            if success:
                logger.info(f"[ReqPool] Added {req_id}")
                return 'added'
            else:
                logger.error(f"[ReqPool] Failed to add {req_id}")
                return 'failed'

        except Exception as e:
            logger.error(f"[ReqPool] Exception saving requirement row: {e}")
            import traceback
            traceback.print_exc()
            return 'failed'

    def _parse_planned_requirements_from_report(self, md_content: str) -> List[str]:
        """
        从周报的Markdown内容中解析"纳入/计划解决的需求"部分
        返回工单ID列表

        Args:
            md_content: 周报Markdown内容

        Returns:
            工单ID列表 (如 ['MYPROJECT-1001', 'OTHERPROJECT-404'])
        """
        # 使用多个模式尝试匹配，提高兼容性
        planned_section_match = None
        for pattern in SECTION_PATTERNS:
            planned_section_match = pattern.search(md_content)
            if planned_section_match:
                logger.info(f"[ReqPool] 使用模式匹配成功: {pattern.pattern[:50]}...")
                break

        if not planned_section_match:
            logger.warning("[ReqPool] 未找到'纳入/计划解决的需求'部分")
            return []

        planned_text = planned_section_match.group(0)
        logger.info(f"[ReqPool] 找到纳入/计划解决的部分，长度: {len(planned_text)}")

        # 提取工单ID (如 MYPROJECT-1001, OTHERPROJECT-404)
        # 匹配格式：**[工单ID]** - 使用精确的工单ID格式 PROJECT-12345
        matches = TICKET_ID_PATTERN.findall(planned_text)

        if not matches:
            logger.warning(f"[ReqPool] 在计划需求部分未找到符合格式的工单ID，尝试宽松匹配...")
            # 降级到宽松匹配：支持 **ID** 或 [ID] 或纯文本 ID
            loose_pattern = re.compile(r'(?:^|\s|-)\*?\*?\[?([A-Z]{2,10}-\d{3,10})\]?\*?\*?(?:\s|：|:|$)')
            matches = loose_pattern.findall(planned_text)

        # 去重并保持顺序
        ticket_ids = list(dict.fromkeys(matches))

        logger.info(f"[ReqPool] 解析到 {len(ticket_ids)} 个计划需求工单: {ticket_ids[:10]}{'...' if len(ticket_ids) > 10 else ''}")
        return ticket_ids

    def _get_latest_weekly_report_file(self) -> Optional[str]:
        """
        获取最新的周报文件（Markdown或JSON）

        Returns:
            最新周报文件路径或None
        """
        if not os.path.exists(REPORT_DIR):
            logger.error(f"[ReqPool] 周报目录不存在: {REPORT_DIR}")
            return None

        # 优先使用标准日期格式的Markdown文件（Weekly_Report_YYYY-MM-DD_YYYY-MM-DD.md）
        import re
        date_pattern = re.compile(r'Weekly_Report_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.md$')
        md_files = []
        for f in os.listdir(REPORT_DIR):
            m = date_pattern.match(f)
            if m:
                md_files.append((m.group(2), f))  # 按结束日期排序
        if not md_files:
            logger.warning(f"[ReqPool] 未找到标准日期格式的周报Markdown文件")
            return None

        md_files.sort(reverse=True)
        latest_file = md_files[0][1]
        filepath = os.path.join(REPORT_DIR, latest_file)
        logger.info(f"[ReqPool] 找到最新周报文件: {latest_file}")

        return filepath

    def _read_file_with_encoding(self, file_path: str) -> Optional[str]:
        """
        尝试多种编码读取文件内容

        Args:
            file_path: 文件路径

        Returns:
            文件内容或None
        """
        encodings = ['utf-8', 'gbk', 'gb2312', 'latin-1', 'utf-16']

        for encoding in encodings:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    return f.read()
            except UnicodeDecodeError:
                logger.debug(f"[ReqPool] 编码 {encoding} 解码失败，尝试下一个...")
                continue
            except Exception as e:
                logger.error(f"[ReqPool] 读取文件失败 ({encoding}): {e}")
                return None

        logger.error(f"[ReqPool] 无法使用任何编码读取文件: {file_path}")
        return None

    def _get_original_csv_from_report(self, report_path: str) -> Optional[str]:
        """
        根据周报文件路径，找到对应的原始CSV文件

        Args:
            report_path: 周报Markdown文件路径

        Returns:
            CSV文件路径或None
        """
        # 从周报文件名中提取源CSV文件名
        # 周报格式: Weekly_Report_20260201.md 或 Weekly_Report_2026-02-01T20_33_13+0800.md
        report_basename = os.path.basename(report_path)

        # 尝试从JSON文件中获取源文件名（如果存在）
        json_path = report_path.replace('.md', '.json')
        source_filename_from_json = None
        if os.path.exists(json_path):
            try:
                content = self._read_file_with_encoding(json_path)
                if content:
                    report_data = json.loads(content)
                    source_filename_from_json = report_data.get('meta', {}).get('filename', '')
                    if source_filename_from_json:
                        csv_path = os.path.join(SRC_DIR, source_filename_from_json)
                        if os.path.exists(csv_path):
                            logger.info(f"[ReqPool] 从JSON找到原始CSV文件: {source_filename_from_json}")
                            return csv_path
                        else:
                            logger.warning(f"[ReqPool] JSON中的CSV文件不存在: {source_filename_from_json}")
            except Exception as e:
                logger.warning(f"[ReqPool] 读取JSON文件失败: {e}")

        # 从周报内容中查找数据源
        content = self._read_file_with_encoding(report_path)
        source_filename_from_md = None
        if content:
            source_match = re.search(r'\*\*数据来源\*\*:\s*`([^`]+)`', content)
            if source_match:
                source_filename_from_md = source_match.group(1)
                csv_path = os.path.join(SRC_DIR, source_filename_from_md)
                if os.path.exists(csv_path):
                    logger.info(f"[ReqPool] 从周报内容找到CSV文件: {source_filename_from_md}")
                    return csv_path
                else:
                    logger.warning(f"[ReqPool] 周报中的CSV文件不存在: {source_filename_from_md}")

        # 如果上述方法都失败，尝试查找文件名变体
        # 使用JSON或周报中找到的文件名尝试变体匹配
        source_filename = source_filename_from_md or source_filename_from_json
        if source_filename:
            logger.info(f"[ReqPool] 尝试查找文件名变体: {source_filename}")
            base_name = source_filename.replace('.csv', '')
            # 生成可能的变体
            possible_variants = [
                source_filename,
                base_name.replace('周数据', '周数据-') + '.csv',  # 在周数据后添加 -
                base_name.replace('工作流', '工作流-') + '.csv',  # 在工作流后添加 -
            ]
            for variant in set(possible_variants):
                variant_path = os.path.join(SRC_DIR, variant)
                if os.path.exists(variant_path):
                    logger.info(f"[ReqPool] 找到变体CSV文件: {variant}")
                    return variant_path

        return None

    def _read_report_content(self, report_file: str) -> Optional[str]:
        """
        读取周报文件内容

        Args:
            report_file: 周报文件路径

        Returns:
            Markdown内容或None
        """
        content = self._read_file_with_encoding(report_file)
        if content:
            logger.info(f"[ReqPool] 成功读取周报文件: {report_file}")
            return content
        else:
            logger.error(f"[ReqPool] 读取周报文件失败: {report_file}")
            return None

    def _load_csv_data(self, csv_file: str, planned_ticket_ids: List[str]) -> Optional[pd.DataFrame]:
        """
        加载CSV数据并筛选计划需求工单

        Args:
            csv_file: CSV文件路径
            planned_ticket_ids: 计划工单ID列表

        Returns:
            筛选后的DataFrame或None
        """
        try:
            df = pd.read_csv(csv_file)
            logger.info(f"[ReqPool] CSV loaded successfully, shape: {df.shape}")
            logger.info(f"[ReqPool] Columns: {list(df.columns)}")
        except Exception as e:
            logger.error(f"[ReqPool] Error loading CSV: {e}")
            return None

        df.columns = [c.strip() for c in df.columns]

        # 根据工单ID筛选数据
        issue_key_col = REPORT_CONFIG['csv_columns']['issue_key']
        if issue_key_col not in df.columns:
            logger.error(f"[ReqPool] Error: '{issue_key_col}' column not found.")
            return None

        # 筛选计划需求中的工单
        planned_df = df[df[issue_key_col].isin(planned_ticket_ids)]
        logger.info(f"[ReqPool] 筛选到 {len(planned_df)} 个计划需求工单 (从CSV中的 {len(planned_ticket_ids)} 个ID)")

        return planned_df

    def _save_requirement(self, row: pd.Series, req_id: str, existing: Optional[Dict]) -> bool:
        """
        保存单个需求到需求池

        Args:
            row: CSV行数据
            req_id: 需求ID
            existing: 已存在的需求数据

        Returns:
            是否保存成功
        """
        issue_key = str(row[REPORT_CONFIG['csv_columns']['issue_key']])
        summary = str(row.get(REPORT_CONFIG['csv_columns']['summary'], ''))
        desc = str(row.get(REPORT_CONFIG['csv_columns']['description'], ''))
        solution = str(row.get(REPORT_CONFIG['csv_columns']['solution'], ''))

        metadata = {
            'status': 'new',
            'source_issues': [issue_key],
            'created_at': existing.get('created_at') if existing else datetime.now().isoformat(),
            'is_planned': True  # 标记为计划需求
        }

        # 把描述和解决方案合并到 description 里
        full_desc = f"【原工单描述】\n{desc}\n\n【处理方案记录】\n{solution}"

        return self.vector_store.upsert_requirement(req_id, summary, full_desc, metadata)

    def _extract_requirements_from_csv(self, csv_file: str) -> Optional[pd.DataFrame]:
        """
        直接从CSV文件中筛选纳入需求的工单
        根据需求文档：筛选"解决方案"字段包含需求关键词，或"研发确认问题类型"为需求的工单

        Args:
            csv_file: CSV文件路径

        Returns:
            筛选后的DataFrame或None
        """
        try:
            df = pd.read_csv(csv_file)
            logger.info(f"[ReqPool] CSV loaded for requirement extraction, shape: {df.shape}")
        except Exception as e:
            logger.error(f"[ReqPool] Error loading CSV: {e}")
            return None

        df.columns = [c.strip() for c in df.columns]

        # 获取列名
        solution_col = REPORT_CONFIG['csv_columns']['solution']
        type_col = REPORT_CONFIG['csv_columns']['type']
        issue_key_col = REPORT_CONFIG['csv_columns']['issue_key']

        # 检查必要列是否存在
        if issue_key_col not in df.columns:
            logger.error(f"[ReqPool] Error: '{issue_key_col}' column not found.")
            return None

        # 条件1: 解决方案包含需求关键词
        def is_requirement_by_solution(solution):
            if pd.isna(solution):
                return False
            solution = str(solution)
            for kw in REPORT_CONFIG['requirement_keywords']:
                try:
                    if re.search(kw, solution, re.IGNORECASE):
                        return True
                except re.error:
                    # 如果正则表达式有问题，使用简单字符串匹配
                    if kw in solution:
                        return True
            return False

        # 条件2: 研发确认问题类型为需求
        def is_requirement_by_type(issue_type):
            if pd.isna(issue_type):
                return False
            return str(issue_type).strip() in REPORT_CONFIG['requirement_types']

        # 应用筛选条件
        if solution_col in df.columns:
            df['is_req_by_solution'] = df[solution_col].apply(is_requirement_by_solution)
        else:
            df['is_req_by_solution'] = False
            logger.warning(f"[ReqPool] Solution column '{solution_col}' not found, skipping solution filter")

        if type_col in df.columns:
            df['is_req_by_type'] = df[type_col].apply(is_requirement_by_type)
        else:
            df['is_req_by_type'] = False
            logger.warning(f"[ReqPool] Type column '{type_col}' not found, skipping type filter")

        # 合并条件（满足任一即可）
        requirement_df = df[df['is_req_by_solution'] | df['is_req_by_type']].copy()

        logger.info(f"[ReqPool] 从CSV中筛选到 {len(requirement_df)} 个需求类工单")

        # 记录匹配原因
        for _, row in requirement_df.iterrows():
            issue_key = row[issue_key_col]
            reasons = []
            if row.get('is_req_by_solution'):
                reasons.append("解决方案")
            if row.get('is_req_by_type'):
                reasons.append("问题类型")
            logger.info(f"[ReqPool]   - {issue_key}: 匹配原因({', '.join(reasons)})")

        return requirement_df

    def extract_from_weekly(self, filepath: str = None) -> int:
        """
        从周报数据中提取需求并存入需求池 (Chroma)。
        根据需求文档：从CSV中筛选"解决方案"包含需求关键词或"研发确认问题类型"为需求的工单。
        同时参考周报Markdown中"纳入/计划解决的需求"部分作为补充验证。

        Args:
            filepath: 指定周报文件路径，为None则使用最新周报

        Returns:
            成功添加的需求数量
        """
        # 1. 获取周报文件
        if not filepath:
            report_file = self._get_latest_weekly_report_file()
            if not report_file:
                logger.error("[ReqPool] 未找到周报文件")
                return 0
        else:
            report_file = filepath

        # 2. 找到对应的原始CSV文件（这是主要数据来源）
        csv_file = self._get_original_csv_from_report(report_file)
        if not csv_file:
            logger.error("[ReqPool] 未找到原始CSV文件")
            return 0

        # 3. 从CSV中直接筛选纳入需求的工单（主要方式）
        requirement_df = self._extract_requirements_from_csv(csv_file)
        if requirement_df is None:
            return 0

        # 4. 同时从周报Markdown解析工单ID（作为补充验证）
        md_content = self._read_report_content(report_file)
        planned_ticket_ids = []
        if md_content:
            planned_ticket_ids = self._parse_planned_requirements_from_report(md_content)
            if planned_ticket_ids:
                logger.info(f"[ReqPool] 从周报Markdown解析到 {len(planned_ticket_ids)} 个工单ID作为补充")

        # 5. 如果有周报解析的额外工单，从CSV加载这些数据并合并
        if planned_ticket_ids:
            # 获取已从CSV筛选的工单ID集合
            issue_key_col = REPORT_CONFIG['csv_columns']['issue_key']
            existing_ids = set(requirement_df[issue_key_col].astype(str).tolist()) if len(requirement_df) > 0 else set()

            # 找出周报有但CSV筛选没有的工单（额外的工单）
            additional_ids = [tid for tid in planned_ticket_ids if tid not in existing_ids]

            if additional_ids:
                logger.info(f"[ReqPool] 周报中有 {len(additional_ids)} 个额外工单不在CSV筛选结果中")
                additional_df = self._load_csv_data(csv_file, additional_ids)
                if additional_df is not None and len(additional_df) > 0:
                    # 合并数据
                    requirement_df = pd.concat([requirement_df, additional_df], ignore_index=True)
                    logger.info(f"[ReqPool] 合并后共 {len(requirement_df)} 个需求工单")

        if len(requirement_df) == 0:
            logger.warning("[ReqPool] 未找到任何需求类工单")
            return 0

        # 6. 提取并存入需求池
        added_count = 0
        skipped_count = 0
        failed_count = 0
        logger.info(f"[ReqPool] Starting to process {len(requirement_df)} requirements...")

        for idx, row in requirement_df.iterrows():
            issue_key = str(row[REPORT_CONFIG['csv_columns']['issue_key']])
            req_id = f"REQ-{issue_key}"

            # 检查是否因为是重复跑而需要跳过
            existing = self.vector_store.get_requirement(req_id)
            if existing and existing.get('status') != 'new':
                logger.info(f"[ReqPool] Skipping {req_id}: existing status={existing.get('status')}")
                skipped_count += 1
                continue

            try:
                success = self._save_requirement(row, req_id, existing)
                if success:
                    added_count += 1
                    logger.info(f"[ReqPool] Added {req_id}")
                else:
                    failed_count += 1
                    logger.error(f"[ReqPool] Failed to add {req_id}")
            except Exception as e:
                failed_count += 1
                logger.error(f"[ReqPool] Exception adding {req_id}: {e}")

        logger.info(f"[ReqPool] Extraction complete: added={added_count}, skipped={skipped_count}, failed={failed_count}, processed={len(requirement_df)}")
        return added_count

    def get_board_data(self, status_filter: str = None, date_range: Dict = None) -> Dict[str, List[Dict]]:
        """
        获取看板各列的数据，支持筛选

        Args:
            status_filter: 按状态筛选（可选）
            date_range: 日期范围筛选（可选）
        """
        # 使用新的list_requirements方法支持日期筛选
        reqs = self.vector_store.list_requirements(status=status_filter, date_range=date_range)

        # 扩展状态列表
        board = {
            'new': [],
            'analyzing': [],
            'to_review': [],
            'drafting': [],
            'draft_review': [],
            'draft_ready': [],
            'scheduled': [],
            'developing': [],      # 新增
            'pending_deploy': [],   # 新增
            'deployed': [],         # 新增
            'rejected': []
        }

        # 批量收集所有 cluster_id，一次性拉回 cluster 元数据（避免 N+1）
        cluster_ids = set()
        for req in reqs:
            ai = req.get('ai_analysis') or {}
            if isinstance(ai, str):
                try: ai = json.loads(ai)
                except: ai = {}
            cid = (ai.get('theme_context') or {}).get('cluster_id')
            if cid:
                cluster_ids.add(cid)

        cluster_map = {}
        if cluster_ids:
            try:
                chroma_ids = [f"cluster_{cid}" for cid in cluster_ids]
                rc = self.vector_store.req_clusters_collection.get(ids=chroma_ids, include=['metadatas'])
                for meta in (rc.get('metadatas') or []):
                    cid = meta.get('cluster_id')
                    if cid:
                        sols = meta.get('solutions')
                        if isinstance(sols, str):
                            try: sols = json.loads(sols)
                            except: sols = []
                        fact = meta.get('requirement_fact_packet')
                        if isinstance(fact, str):
                            try: fact = json.loads(fact)
                            except: fact = {}
                        member_ids = meta.get('member_req_ids')
                        if isinstance(member_ids, str):
                            try: member_ids = json.loads(member_ids)
                            except: member_ids = []
                        cluster_map[cid] = {
                            'cluster_id':        cid,
                            'topic_name':        meta.get('title'),
                            'topic_l1':          meta.get('topic_l1'),
                            'topic_l2':          meta.get('topic_l2'),
                            'cluster_status':    meta.get('status'),
                            'solutions_ready':   bool(meta.get('solutions_ready')),
                            'solutions_count':   len(sols) if isinstance(sols, list) else 0,
                            'top_solution':      (sols[0] if isinstance(sols, list) and sols else None),
                            'fact_packet':       fact,
                            'member_count':      len(member_ids) if isinstance(member_ids, list) else 0,
                            'value_score':       meta.get('value_score'),
                            'commonality_score': meta.get('commonality_score'),
                        }
            except Exception as e:
                logger.warning(f"[get_board_data] cluster batch fetch failed: {e}")

        for req in reqs:
            if req.get('ai_analysis'):
                req['ai_analysis'] = self._normalize_ai_analysis(req['ai_analysis'])
            status = self._normalize_requirement_status(req)
            req['status'] = status
            # 贴上 cluster 摘要
            ai = req.get('ai_analysis') or {}
            cid = (ai.get('theme_context') or {}).get('cluster_id')
            if cid and cid in cluster_map:
                req['cluster_brief'] = cluster_map[cid]
            if status in board:
                board[status].append(req)
            else:
                board['new'].append(req)

        return board

    def update_status(self, req_id: str, new_status: str) -> bool:
        """更新需求的状态"""
        req = self.vector_store.get_requirement(req_id)
        if not req:
            return False
            
        req['status'] = new_status
        req['updated_at'] = datetime.now().isoformat()
        
        # 将结构化的字段回填 metadata
        return self.vector_store.upsert_requirement(req_id, req['title'], req['description'], self._build_requirement_metadata(req))

    def submit_review(self, req_id: str, reviewer: str, decision: str,
                      comments: str, expected_version: str = "") -> bool:
        """
        提交质量委员会评审

        Args:
            req_id: 需求ID
            reviewer: 评审人
            decision: 'approve'|'reject'|'return'|'schedule'|'start_develop'|'ready_deploy'|'deploy'
            comments: 评审意见
            expected_version: 预期版本
        """
        req = self.vector_store.get_requirement(req_id)
        if not req:
            return False

        record = {
            'reviewer': reviewer,
            'decision': decision,
            'comments': comments,
            'expected_version': expected_version,
            'timestamp': datetime.now().isoformat()
        }

        # Initialize if missing
        if not isinstance(req.get('review_records'), list):
            req['review_records'] = []

        req['review_records'].append(record)

        # 根据决策自动流转状态
        status_map = {
            'approve': 'scheduled',
            'schedule': 'scheduled',
            'reject': 'rejected',
            'return': 'new',
            'start_develop': 'developing',
            'ready_deploy': 'pending_deploy',
            'deploy': 'deployed'
        }

        new_status = status_map.get(decision, req.get('status'))
        req['status'] = new_status
        req['updated_at'] = datetime.now().isoformat()

        return self.vector_store.upsert_requirement(req_id, req['title'], req['description'], self._build_requirement_metadata(req))

    def trigger_analysis(self, req_id: str, llm_config: Dict = None) -> bool:
        """触发 Subagent 进行需求深度分析

        Args:
            req_id: 需求ID
            llm_config: LLM配置，包含provider, apiKey, modelName, baseUrl
        """
        req = self.vector_store.get_requirement(req_id)
        if not req:
            return False
        if self.draft_service:
            self.start_analysis_task(req_id, llm_config)
            return True

        self.update_status(req_id, 'drafting')
        return self._execute_analysis(req_id, llm_config)

    def _execute_analysis(self, req_id: str, llm_config: Dict = None) -> bool:
        req = self.vector_store.get_requirement(req_id)
        if not req:
            return False

        try:
            analysis_result = self._normalize_ai_analysis(self.agent.analyze(req, llm_config))
            evidence_bundle = self._build_analysis_evidence_bundle(req, analysis_result, llm_config)
            topic_names = evidence_bundle.get('topic_names', [])
            if evidence_bundle:
                analysis_result = {
                    **analysis_result,
                    'module_hint': analysis_result.get('module', ''),
                    'topic_ids': evidence_bundle.get('topic_ids', []),
                    'topic_names': topic_names,
                    'evidence_bundle': evidence_bundle,
                }
            if self.design_fact_service:
                knowledge_context = self.design_fact_service.build_requirement_context(
                    {**req, 'ai_analysis': analysis_result},
                    evidence_bundle=evidence_bundle,
                )
                analysis_result = {
                    **analysis_result,
                    'design_fact_bundle': knowledge_context.get('design_fact_bundle', {}),
                    'competitor_dossiers': knowledge_context.get('competitor_dossiers', []),
                }

            # 查询 KB 综合解析条目，注入产品现状上下文
            compiled_context = []
            duplicate_signal = None
            try:
                if self.kb_runtime_service:
                    title = req.get('title', '')
                    desc = req.get('description', '')[:200] if req.get('description') else ''
                    query = f"{title} {desc}".strip()
                    hits = self.kb_runtime_service.search(
                        query, top_k=3, source_kind='kb_compiled'
                    )
                    for h in hits:
                        compiled_context.append({
                            'topic': h.get('name', ''),
                            'preview': h.get('chunk_preview', h.get('chunk_text', ''))[:300],
                        })
                        # 去重信号：KB中已有"计划中"或"已实现"标记
                        chunk_text = h.get('chunk_text', h.get('chunk_preview', ''))
                        if any(kw in chunk_text for kw in ['计划中', '已实现', '已在路线图', 'status: 计划中']):
                            if duplicate_signal is None:
                                duplicate_signal = {
                                    'topic': h.get('name', ''),
                                    'hint': '该功能区域已有规划或实现'
                                }
            except Exception:
                pass  # KB 查询失败不影响主流程
            analysis_result['compiled_context'] = compiled_context
            analysis_result['duplicate_signal'] = duplicate_signal

            # 3. 更新分析结果并流转到统一的 draft_review
            req['ai_analysis'] = analysis_result
            req['status'] = 'draft_review'
            req['feishu_notified'] = False

            self.vector_store.upsert_requirement(req_id, req['title'], req['description'], self._build_requirement_metadata(req))
            return True
        except Exception as e:
            logger.error(f"[ReqPool] Analysis failed: {e}")
            self.update_status(req_id, 'new') # 失败回退
            return False

    def start_analysis_task(self, req_id: str, llm_config: Dict = None) -> str:
        req = self.vector_store.get_requirement(req_id)
        if not req:
            raise ValueError("Requirement not found.")

        if self.draft_service:
            draft_task_id = self.draft_service.start_generation_task(req_id, "summary", llm_config=llm_config or {})
            task_id = str(uuid.uuid4())
            self.analysis_tasks[task_id] = {
                "task_id": task_id,
                "req_id": req_id,
                "draft_task_id": draft_task_id,
                "status": "pending",
                "resulting_requirement_status": "drafting",
                "error": "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            return task_id

        self.update_status(req_id, 'drafting')

        task_id = str(uuid.uuid4())
        self.analysis_tasks[task_id] = {
            "task_id": task_id,
            "req_id": req_id,
            "status": "pending",
            "resulting_requirement_status": "drafting",
            "error": "",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

        worker = threading.Thread(target=self._run_analysis_task, args=(task_id, llm_config or {}), daemon=True)
        worker.start()
        return task_id

    def _run_analysis_task(self, task_id: str, llm_config: Dict):
        task = self.analysis_tasks[task_id]
        task["status"] = "running"
        task["updated_at"] = datetime.now().isoformat()

        ok = self._execute_analysis(task["req_id"], llm_config)
        task["status"] = "completed" if ok else "failed"
        task["resulting_requirement_status"] = "draft_review" if ok else "new"
        task["error"] = "" if ok else "Analysis failed"
        task["updated_at"] = datetime.now().isoformat()

    def get_analysis_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self.analysis_tasks.get(task_id)
        if not task:
            return None
        draft_task_id = task.get("draft_task_id")
        if draft_task_id and self.draft_service:
            draft_task = self.draft_service.get_task_status(draft_task_id)
            if draft_task:
                status = draft_task.get("status", "pending")
                resulting_status = "drafting"
                if status == "completed":
                    resulting_status = ((draft_task.get("artifact") or {}).get("draft_status") or "draft_review")
                elif status == "failed":
                    req = self.vector_store.get_requirement(task["req_id"]) or {}
                    resulting_status = self._normalize_requirement_status(req)
                return {
                    **task,
                    "status": status,
                    "resulting_requirement_status": resulting_status,
                    "error": draft_task.get("error", ""),
                    "updated_at": datetime.now().isoformat(),
                }
        return task

    def _normalize_requirement_status(self, req: Dict[str, Any]) -> str:
        status = req.get("status", "new")
        ai_analysis = req.get("ai_analysis", {}) or {}
        if status == "analyzing":
            return "drafting"
        if status == "to_review":
            return "draft_review" if ai_analysis.get("latest_draft_id") else "new"
        return status

    def get_all_requirements(self) -> List[Dict]:
        """获取所有需求（用于调试/管理）"""
        return self.vector_store.list_requirements()

    def get_requirements_by_status(self, status: str = None, date_range: Dict = None) -> Dict[str, List[Dict]]:
        """按状态获取需求统计，支持日期范围筛选"""
        all_reqs = self.vector_store.list_requirements(date_range=date_range)
        if status:
            return {status: [r for r in all_reqs if r.get('status') == status]}
        # 返回所有状态的分布（包含新增状态）
        result = {
            'new': [],
            'analyzing': [],
            'to_review': [],
            'drafting': [],
            'draft_review': [],
            'draft_ready': [],
            'scheduled': [],
            'developing': [],      # 新增
            'pending_deploy': [],   # 新增
            'deployed': [],         # 新增
            'rejected': []
        }
        for req in all_reqs:
            if req.get('ai_analysis'):
                req['ai_analysis'] = self._normalize_ai_analysis(req['ai_analysis'])
            s = self._normalize_requirement_status(req)
            req['status'] = s
            if s in result:
                result[s].append(req)
            else:
                result['new'].append(req)
        return result

    def delete_requirement(self, req_id: str) -> bool:
        """删除指定的需求"""
        return self.vector_store.delete_requirement(req_id)

    def clear_all_requirements(self) -> int:
        """清空所有需求（谨慎使用，用于重新生成）"""
        all_reqs = self.get_all_requirements()
        count = len(all_reqs)
        success = self.vector_store.clear_requirements()
        if success:
            logger.info(f"[ReqPool] 已清空 {count} 条需求记录")
        return count if success else 0

    def get_stats(self, date_range: Dict = None) -> Dict:
        """获取需求池统计信息，支持日期范围筛选"""
        by_status = self.get_requirements_by_status(date_range=date_range)
        return {
            'total': sum(len(v) for v in by_status.values()),
            'by_status': {k: len(v) for k, v in by_status.items()},
            'statuses': by_status
        }

    async def analyze_batch(self, req_ids: List[str], llm_config: Dict) -> Dict:
        """
        批量分析多个需求

        Args:
            req_ids: 需求ID列表
            llm_config: LLM配置

        Returns:
            分析结果
        """
        # 1. 获取所有需求的详细信息，守门：只接受 status='new'
        requirements = []
        skipped = []
        for req_id in req_ids:
            req = self.vector_store.get_requirement(req_id)
            if not req:
                skipped.append({"req_id": req_id, "reason": "not_found"})
                continue
            if req.get("status") != "new":
                skipped.append({"req_id": req_id, "reason": f"status={req.get('status')},refused"})
                continue
            # 通过守门后才翻状态
            self.update_status(req_id, 'drafting')
            requirements.append(req)

        if not requirements:
            return {"error": "没有可分析的新需求", "skipped": skipped}

        # 2. 调用Agent进行批量分析
        try:
            result = await self.agent.analyze_batch(requirements, llm_config)

            # 3. 保存分析结果到各个需求
            for req_id, individual in result.get('individual_analysis', {}).items():
                req = self.vector_store.get_requirement(req_id)
                if req:
                    req['ai_analysis'] = {
                        'batch_context': result.get('batch_analysis', {}),
                        'individual': individual,
                        'analyzed_at': datetime.now().isoformat()
                    }
                    req['status'] = 'draft_review'
                    req['feishu_notified'] = False
                    req['updated_at'] = datetime.now().isoformat()

                    self.vector_store.upsert_requirement(
                        req_id, req['title'], req['description'], self._build_requirement_metadata(req)
                    )

            result["skipped"] = skipped
            return result
        except Exception as e:
            # 分析失败，回退状态（只回退我们翻过的 new → drafting）
            for req in requirements:
                self.update_status(req['req_id'], 'new')
            raise e

    def get_pending_notify(self) -> list:
        """返回分析完成但尚未推送飞书的需求列表"""
        all_reqs = self.vector_store.list_requirements()
        return [
            r for r in all_reqs
            if r.get('status') == 'draft_review'
            and not r.get('feishu_notified', False)
            and r.get('ai_analysis')
        ]

    def mark_feishu_notified(self, req_id: str) -> bool:
        """标记需求已推送飞书"""
        req = self.vector_store.get_requirement(req_id)
        if not req:
            return False
        req['feishu_notified'] = True
        req['feishu_notified_at'] = datetime.now().isoformat()
        self.vector_store.upsert_requirement(
            req_id, req['title'], req['description'],
            self._build_requirement_metadata(req)
        )
        return True

    def export_to_markdown(self, req_ids: List[str] = None,
                           status_filter: str = None,
                           output_path: str = None) -> str:
        """
        将需求导出为Markdown文档

        Args:
            req_ids: 指定需求ID列表（可选）
            status_filter: 按状态筛选（可选）
            output_path: 输出文件路径（可选，默认保存到conclusion/requirements/）

        Returns:
            生成的文件路径
        """
        # 1. 获取要导出的需求
        if req_ids:
            requirements = [self.vector_store.get_requirement(rid) for rid in req_ids]
            requirements = [r for r in requirements if r]
        else:
            requirements = self.vector_store.list_requirements(status=status_filter)

        if not requirements:
            raise ValueError("没有符合条件的需求")

        # 2. 构建Markdown内容
        md_content = self._build_requirement_doc(requirements)

        # 3. 确定输出路径
        if not output_path:
            output_dir = os.path.join(PROJECT_ROOT, "conclusion/requirements")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(output_dir, f"requirement-pool-{timestamp}.md")

        # 4. 写入文件
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(md_content)

        return output_path

    def _build_requirement_doc(self, requirements: List[Dict]) -> str:
        """构建需求文档内容"""

        doc = f"""# 需求池分析报告

> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
> 需求数量: {len(requirements)}

## 目录

1. [需求清单](#需求清单)
2. [整体分析](#整体分析)
3. [详细需求](#详细需求)
4. [实施规划](#实施规划)

---

## 需求清单

| 序号 | 需求ID | 标题 | 状态 | 创建时间 |
|------|--------|------|------|----------|
"""

        for i, req in enumerate(requirements, 1):
            status_label = REQUIREMENT_STATUSES.get(req['status'], req['status'])
            doc += f"| {i} | {req['req_id']} | {req['title']} | {status_label} | {req.get('created_at', '')[:10]} |\n"

        # 添加整体分析（如果有批量分析结果）
        doc += "\n## 整体分析\n\n"

        # 按状态分组统计
        status_count = {}
        for req in requirements:
            status = req.get('status', 'new')
            status_count[status] = status_count.get(status, 0) + 1

        doc += "### 状态分布\n\n"
        for status, count in status_count.items():
            label = REQUIREMENT_STATUSES.get(status, status)
            doc += f"- {label}: {count} 条\n"

        # 添加详细需求
        doc += "\n## 详细需求\n\n"

        for req in requirements:
            ai_analysis = req.get('ai_analysis', {})
            individual = ai_analysis.get('individual', {})
            batch_context = ai_analysis.get('batch_context', {})

            doc += f"""### {req['req_id']}: {req['title']}

**状态**: {REQUIREMENT_STATUSES.get(req['status'], req['status'])}

**原始描述**:
{req.get('description', '无')}

**AI分析结果**:

- **根因分析**: {individual.get('root_cause', '未分析')}
- **归属模块**: {individual.get('module', '未分析')}
- **落地方案**: {individual.get('detailed_solution', '未分析')}
- **MVP建议**: {individual.get('mvp_suggestion', '未分析')}
- **工时预估**: {individual.get('effort_estimation', '未分析')}

**评审记录**:

"""
            if req.get('review_records'):
                for record in req['review_records']:
                    doc += f"- {record.get('timestamp', '')[:10]} {record.get('reviewer', '')}: {record.get('decision', '')} - {record.get('comments', '')}\n"
            else:
                doc += "暂无评审记录\n"

            doc += "\n---\n\n"

        # 添加实施规划（从批量分析结果中提取）
        doc += "## 实施规划\n\n"

        # 尝试从第一个需求的批量分析结果中提取实施阶段
        if requirements and requirements[0].get('ai_analysis', {}).get('batch_context', {}).get('implementation_phases'):
            phases = requirements[0]['ai_analysis']['batch_context']['implementation_phases']
            for phase in phases:
                doc += f"""### 阶段{phase.get('phase', '')}: {phase.get('phase_name', '')}

- **里程碑**: {phase.get('milestone', '')}
- **包含需求**: {', '.join(phase.get('requirements', []))}
- **预估工时**: {phase.get('estimated_effort', '')}

"""
        else:
            doc += "暂无实施规划数据\n"

        return doc

    def batch_update_status(self, req_ids: List[str], new_status: str) -> int:
        """
        批量更新需求状态

        Args:
            req_ids: 需求ID列表
            new_status: 新状态

        Returns:
            成功更新的数量
        """
        success_count = 0
        for req_id in req_ids:
            if self.update_status(req_id, new_status):
                success_count += 1
        return success_count


# Module-level registry so routers can get the service instance set by main.py
_req_pool_service_instance = None


def register_req_pool_service(svc: "RequirementsPoolService") -> None:
    global _req_pool_service_instance
    _req_pool_service_instance = svc


def get_req_pool_service() -> "RequirementsPoolService":
    return _req_pool_service_instance
