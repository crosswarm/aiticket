import json
import os
import asyncio
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from agents.base import BaseAgent
from agents.self_monitor_mixin import AgentSelfMonitorMixin
from llm_service import LLMService
from vector_store import VectorStore

_LLM_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'llm_config.json')
# 可进化 prompt 模板目录（evolution_core 通过修改这些 .md 文件进化 req_analyst 的分析行为）
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "req_analyst_prompts"


def _load_prompt_template(filename: str) -> str:
    """从 data/req_analyst_prompts/ 加载 prompt 模板，找不到时返回空串（调用方负责降级）。"""
    path = _PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _load_default_llm_config() -> dict:
    """读取 llm_config.json，优先使用 feature routing 中 req_analysis 指定的 provider"""
    try:
        with open(_LLM_CONFIG_PATH, encoding='utf-8') as f:
            full = json.load(f)

        routing_path = os.path.join(os.path.dirname(__file__), '..', 'llm_feature_routing.json')
        routing = {}
        if os.path.exists(routing_path):
            try:
                with open(routing_path, encoding='utf-8') as rf:
                    routing = json.load(rf)
            except Exception:
                pass

        provider = routing.get('req_analysis') or routing.get('_default') or full.get('last_provider', '')
        if not provider or provider == 'none':
            return {}
        pcfg = full.get(provider, {})
        return {
            'provider': provider,
            'apiKey': pcfg.get('api_key', ''),
            'modelName': pcfg.get('model_name', ''),
            'baseUrl': pcfg.get('base_url', ''),
        }
    except Exception:
        return {}


class ReqAnalystAgent(AgentSelfMonitorMixin, BaseAgent):
    expected_run_interval_hours: float = 24
    name = "req_analyst"
    display_name = "需求分析 Agent"
    description = "批量分析需求池，发现关联、冲突与依赖，生成结构化分析报告"
    version = "1.0"

    def __init__(self, vector_store: VectorStore):
        super().__init__()
        self.vector_store = vector_store
        self.llm_service = LLMService()

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
        }

    def list_capabilities(self) -> list:
        return ["req-analysis", "llm-batch"]

    def health_check(self) -> dict:
        return {"healthy": True, "detail": "ok"}

    async def analyze_batch(self, requirements: List[Dict], llm_config: Dict) -> Dict[str, Any]:
        """
        批量整体分析多个需求

        采用整体思维，分析需求之间的关系、关联、冲突、依赖等

        Args:
            requirements: 需求列表
            llm_config: LLM配置

        Returns:
            包含整体分析和个体分析的结果
        """
        if not requirements:
            return {"error": "没有需求需要分析"}

        # 1. 构建整体分析Prompt
        batch_prompt = self._build_batch_analysis_prompt(requirements)

        # 2. 调用LLM进行整体分析
        batch_result = await self._call_llm_async(batch_prompt, llm_config)

        # 3. 解析整体分析结果
        parsed_batch = self._parse_batch_result(batch_result)

        # 4. 对每个需求组进行个体详细分析
        individual_results = {}
        aggregated_groups = parsed_batch.get('aggregated_groups', [])

        if aggregated_groups:
            for group in aggregated_groups:
                for req_id in group.get('requirement_ids', []):
                    req = next((r for r in requirements if r['req_id'] == req_id), None)
                    if req:
                        individual_prompt = self._build_individual_prompt(
                            req, group, parsed_batch
                        )
                        individual_result = await self._call_llm_async(individual_prompt, llm_config)
                        individual_results[req_id] = self._parse_individual_result(individual_result)
        else:
            # 如果没有分组，单独分析每个需求
            for req in requirements:
                individual_prompt = self._build_single_analysis_prompt(req, parsed_batch)
                individual_result = await self._call_llm_async(individual_prompt, llm_config)
                individual_results[req['req_id']] = self._parse_individual_result(individual_result)

        return {
            'batch_analysis': parsed_batch,
            'individual_analysis': individual_results,
            'analysis_timestamp': datetime.now().isoformat(),
            'total_requirements': len(requirements)
        }

    def _build_batch_analysis_prompt(self, requirements: List[Dict]) -> str:
        """构建批量分析Prompt（静态指令从 data/req_analyst_prompts/batch_analysis.md 加载）"""
        reqs_text = ""
        for i, req in enumerate(requirements, 1):
            reqs_text += f"\n【需求{i}】\n"
            reqs_text += f"ID: {req['req_id']}\n"
            reqs_text += f"标题: {req['title']}\n"
            reqs_text += f"描述: {req.get('description', '')[:500]}\n"

        tpl = _load_prompt_template("batch_analysis.md")
        if tpl:
            return tpl.replace("__REQS_TEXT__", reqs_text)

        # 降级：模板文件缺失时使用内联字符串（行为与原代码完全一致）
        return f"""你是一位资深的产品架构师，需要对以下多个需求进行整体分析。
请从系统性和关联性角度审视这些需求，而不是孤立地看待每一个。

{reqs_text}

请输出以下维度的分析结果（JSON格式）：

{{
    "overall_architecture": {{
        "summary": "整体需求架构描述",
        "module_division": ["模块1", "模块2"],
        "boundary_definition": "各模块边界定义"
    }},
    "aggregated_groups": [
        {{
            "group_id": "G1",
            "group_name": "分组名称",
            "requirement_ids": ["REQ-XXX", "REQ-YYY"],
            "reason": "聚合原因",
            "unified_design": "统一设计方案"
        }}
    ],
    "conflicts": [
        {{
            "type": "功能冲突|流程冲突|数据冲突",
            "between": ["REQ-XXX", "REQ-YYY"],
            "description": "冲突描述",
            "resolution": "解决建议"
        }}
    ],
    "dependencies": [
        {{
            "from": "REQ-XXX",
            "to": "REQ-YYY",
            "type": "强依赖|弱依赖",
            "reason": "依赖原因"
        }}
    ],
    "implementation_phases": [
        {{
            "phase": 1,
            "phase_name": "阶段名称",
            "requirements": ["REQ-XXX", "REQ-YYY"],
            "milestone": "阶段里程碑",
            "estimated_effort": "预估工时"
        }}
    ],
    "value_analysis": {{
        "overall_business_value": "整体业务价值",
        "priority_ranking": ["REQ-XXX", "REQ-YYY"],
        "roi_assessment": "ROI评估"
    }},
    "scenario_analysis": {{
        "end_to_end_process": "端到端业务流程",
        "user_journey": "用户旅程",
        "scenario_coverage": ["场景1", "场景2"]
    }},
    "competitive_analysis": {{
        "market_positioning": "市场定位",
        "differentiation_strategy": "差异化策略",
        "competitor_comparison": "竞品对比"
    }},
    "technical_solution": {{
        "technology_selection": "技术选型建议",
        "implementation_difficulty": "实现难度评估",
        "risk_assessment": "风险评估"
    }},
    "data_migration_plan": {{
        "migration_strategy": "数据迁移策略",
        "compatibility": "兼容性考虑",
        "rollback_plan": "回滚方案"
    }}
}}

请确保输出是合法的JSON格式，不要包含任何markdown标记或额外说明。"""

    def _build_individual_prompt(self, req: Dict, group: Dict, batch_result: Dict) -> str:
        """构建个体需求分析Prompt（静态指令从 data/req_analyst_prompts/individual_analysis.md 加载）"""
        tpl = _load_prompt_template("individual_analysis.md")
        if tpl:
            return (tpl
                    .replace("__GROUP_NAME__", group.get('group_name', ''))
                    .replace("__UNIFIED_DESIGN__", group.get('unified_design', ''))
                    .replace("__REQ_ID__", req['req_id'])
                    .replace("__REQ_TITLE__", req['title'])
                    .replace("__REQ_DESCRIPTION__", req.get('description', '')[:1000]))

        # 降级：模板文件缺失时使用内联字符串
        return f"""基于以下整体分析结果，对单个需求进行详细分析：

【整体上下文】
所属分组: {group.get('group_name', '')}
分组设计: {group.get('unified_design', '')}

【当前需求】
ID: {req['req_id']}
标题: {req['title']}
描述: {req.get('description', '')[:1000]}

【分析要求】
请结合整体架构和分组设计，先判断"当前产品已有能力 → 为什么没满足 → 应优先在哪一层承接"，再输出以下 JSON：

{{
    "core_problem": "一句话说明用户真正要解决的核心问题",
    "current_product_behavior": "现有产品在该场景下已提供的能力、交互或配置方式",
    "gap_analysis": "为什么现有能力没有满足诉求，说明是交互缺失/功能缺失/配置缺失/流程设计问题中的哪类缺口",
    "root_cause": "综合根因分析",
    "module": "所属模块",
    "product_layer": "运行时|租户级|流程级|节点级|跨层",
    "scenario_keywords": ["关键场景词1", "关键场景词2"],
    "detailed_solution": "详细落地方案",
    "interface_design": "接口设计建议",
    "data_model": "数据模型建议",
    "mvp_suggestion": "MVP建议",
    "effort_estimation": "工时预估",
    "acceptance_criteria": ["验收标准1", "验收标准2"]
}}

请确保输出是合法的JSON格式。"""

    def _build_single_analysis_prompt(self, req: Dict, batch_result: Dict) -> str:
        """构建单个需求分析Prompt（静态指令从 data/req_analyst_prompts/single_analysis.md 加载）"""
        overall = batch_result.get('overall_architecture', {})
        tpl = _load_prompt_template("single_analysis.md")
        if tpl:
            return (tpl
                    .replace("__OVERALL_SUMMARY__", overall.get('summary', ''))
                    .replace("__MODULE_DIVISION__", ', '.join(overall.get('module_division', [])))
                    .replace("__REQ_ID__", req['req_id'])
                    .replace("__REQ_TITLE__", req['title'])
                    .replace("__REQ_DESCRIPTION__", req.get('description', '')[:1000]))

        # 降级：模板文件缺失时使用内联字符串
        return f"""基于以下整体分析结果，对单个需求进行详细分析：

【整体上下文】
整体架构: {overall.get('summary', '')}
模块划分: {', '.join(overall.get('module_division', []))}

【当前需求】
ID: {req['req_id']}
标题: {req['title']}
描述: {req.get('description', '')[:1000]}

【分析要求】
请先判断"当前产品已有能力是什么、差距在哪里、优先应该落在哪个产品层级"，再输出以下 JSON：

{{
    "core_problem": "一句话说明用户真正要解决的核心问题",
    "current_product_behavior": "现有产品在该场景下已提供的能力、交互或配置方式",
    "gap_analysis": "为什么现有能力没有满足诉求，说明是交互缺失/功能缺失/配置缺失/流程设计问题中的哪类缺口",
    "root_cause": "综合根因分析",
    "module": "所属模块",
    "product_layer": "运行时|租户级|流程级|节点级|跨层",
    "scenario_keywords": ["关键场景词1", "关键场景词2"],
    "detailed_solution": "详细落地方案",
    "interface_design": "接口设计建议",
    "data_model": "数据模型建议",
    "mvp_suggestion": "MVP建议",
    "effort_estimation": "工时预估",
    "acceptance_criteria": ["验收标准1", "验收标准2"]
}}

请确保输出是合法的JSON格式。"""

    async def _call_llm_async(self, prompt: str, llm_config: Dict) -> str:
        """异步调用LLM"""
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(
                pool,
                self._call_llm_sync,
                prompt,
                llm_config
            )
        return result

    def _call_llm_sync(self, prompt: str, llm_config: Dict) -> str:
        """同步调用LLM（在线程池中运行）"""
        # 支持 camelCase 和 snake_case，空时读 llm_config.json
        api_key = llm_config.get('apiKey') or llm_config.get('api_key', '')
        if not api_key:
            file_cfg = _load_default_llm_config()
            llm_config = {**file_cfg, **llm_config}
            api_key = llm_config.get('apiKey', '')
        provider = llm_config.get('provider', 'gemini')
        model_name = llm_config.get('modelName') or llm_config.get('model_name', '')
        base_url = llm_config.get('baseUrl') or llm_config.get('base_url', '')

        # 根据provider设置默认model_name
        if not model_name:
            if provider == "gemini":
                model_name = "gemini-2.5-pro"
            elif provider == "openai":
                model_name = "gpt-4"
            elif provider == "deepseek":
                model_name = "deepseek-chat"

        return self.llm_service.call_llm(
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            model_name=model_name,
            base_url=base_url if base_url else None
        )

    def _parse_batch_result(self, raw_response: str) -> Dict:
        """解析批量分析结果"""
        try:
            return self._parse_json_payload(raw_response)
        except Exception as e:
            print(f"[ReqAnalystAgent] 解析批量分析结果失败: {e}")
            return {}

    def _parse_individual_result(self, raw_response: str) -> Dict:
        """解析个体分析结果"""
        return self._parse_batch_result(raw_response)

    def _strip_model_wrappers(self, raw_response: str) -> str:
        """去掉常见的思维标记、markdown 围栏和多余说明。"""
        if not raw_response:
            return ""
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_response, flags=re.IGNORECASE)
        cleaned = re.sub(r"```(?:json|markdown|md)?", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace("```", "")
        return cleaned.strip()

    def _extract_first_json_object(self, text: str) -> str:
        """提取首个完整 JSON object。"""
        start = text.find("{")
        if start == -1:
            return ""

        depth = 0
        in_string = False
        escape = False
        for index, char in enumerate(text[start:], start=start):
            if escape:
                escape = False
                continue
            if char == "\\" and in_string:
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        return ""

    def _looks_like_error_response(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in [
                "error code",
                "invalid_authentication_error",
                "access_terminated_error",
                "api key",
                "authentication",
                "unauthorized",
                "forbidden",
                "rate limit",
                "timeout",
            ]
        )

    def _parse_json_payload(self, raw_response: str) -> Dict:
        cleaned = self._strip_model_wrappers(raw_response)
        if not cleaned:
            raise ValueError("empty response")

        json_block = self._extract_first_json_object(cleaned) or cleaned
        return json.loads(json_block)

    def _build_analysis_fallback(self, message: str, merge_recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "core_problem": message,
            "current_product_behavior": "待确认，当前未获得足够结构化分析信息。",
            "gap_analysis": message,
            "root_cause": message,
            "module": "待确认",
            "product_layer": "待确认",
            "scenario_keywords": [],
            "mvp_suggestion": "本次自动分析未形成可用结论，请检查模型配置或稍后重试。",
            "merge_recommendation": merge_recommendations,
        }

    def analyze(self, req: Dict[str, Any], llm_config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        核心智能分析方法：
        1. 寻找相似工单建议合并。
        2. 给 LLM 提供 Prompt，得到 根因、模块归属、落地方案。

        Args:
            req: 需求数据
            llm_config: LLM配置，包含provider, apiKey, modelName, baseUrl
        """
        # Step 1: 扫描相似需求
        similar_reqs = self.vector_store.search_similar_requirements(
            query=req.get('description', '')[:1000],
            top_k=3
        )

        # 过滤掉自身，阈值可根据实际调整 (类似 Chroma 余弦相似度)
        merge_recommendations = [
            {'req_id': s['req_id'], 'title': s['title'], 'score': s['score'], 'status': s['status']}
            for s in similar_reqs if s['req_id'] != req['req_id'] and s['score'] > 0.8
        ]

        # Step 2: 获取LLM配置（优先使用传入的配置，否则依次尝试环境变量和 llm_config.json）
        # 同时支持 camelCase (apiKey) 和 snake_case (api_key) 两种格式
        if llm_config and (llm_config.get('apiKey') or llm_config.get('api_key')):
            provider = llm_config.get('provider', 'gemini')
            api_key = llm_config.get('apiKey') or llm_config.get('api_key', '')
            model_name = llm_config.get('modelName') or llm_config.get('model_name', '')
            base_url = llm_config.get('baseUrl') or llm_config.get('base_url', '')
        else:
            # 向后兼容：使用环境变量
            provider = os.environ.get("LLM_PROVIDER", "")
            api_key = os.environ.get("LLM_API_KEY", "")
            model_name = os.environ.get("LLM_MODEL_NAME", "")
            base_url = os.environ.get("LLM_BASE_URL", "")
            # 最终降级：读取 llm_config.json
            if not api_key:
                file_cfg = _load_default_llm_config()
                provider = file_cfg.get('provider', provider or 'gemini')
                api_key = file_cfg.get('apiKey', '')
                model_name = file_cfg.get('modelName', model_name)
                base_url = file_cfg.get('baseUrl', base_url)

        if not api_key:
            return {
                "core_problem": "API Key 未配置，无法进行结构化需求分析。请在设置中配置大模型API Key。",
                "current_product_behavior": "待确认，因模型未实际执行分析。",
                "gap_analysis": "缺少模型调用能力，无法识别当前产品行为与需求缺口。",
                "root_cause": "API Key 未配置，无法进行根因分析。请在设置中配置大模型API Key。",
                "module": "未知",
                "product_layer": "待确认",
                "scenario_keywords": [],
                "merge_recommendation": merge_recommendations,
                "mvp_suggestion": "API Key 未配置，无法提供MVP建议。请在设置中配置大模型API Key。"
            }

        prompt = f"""你现在是一个具备多年经验的高级产品兼研发架构师。
请你对下方被认定为"产品需求"的用户工单记录进行深层次剖析。

【需求基础信息】
摘要: {req.get('title', '')}
详情及过往处理:
{req.get('description', '')[:2000]}

【分析框架】
请先判断：
1. 当前产品在这个场景下已经有什么能力、页面、配置或流程。
2. 用户为什么仍然不满足，是交互缺失、功能缺失、配置缺失还是流程设计缺陷。
3. 这个需求主要应该落在哪个产品层级：运行时、租户级、流程级、节点级；若跨多个层级则写"跨层"。
4. 提炼 3~8 个最能代表该业务问题的场景关键词，优先输出可用于检索相似工单和能力资料的词。

【你必须输出】
请只输出一个合法的 JSON 数据，不要包含任何 markdown 或解释：
{{
    "core_problem": "一句话说明用户真正抱怨/诉求的核心问题",
    "current_product_behavior": "现有产品在该场景下已提供的能力、交互或配置方式",
    "gap_analysis": "现有能力为什么没有满足诉求，要明确属于交互缺失/功能缺失/配置缺失/流程设计缺陷中的哪类",
    "root_cause": "判断用户提这个需求的深层次原因",
    "module": "预估该需求发生在此系统的哪一个大模块/应用中（如审批流、表单引擎、用户权限、列表视图等）",
    "product_layer": "运行时|租户级|流程级|节点级|跨层",
    "scenario_keywords": ["关键场景词1", "关键场景词2", "关键场景词3"],
    "mvp_suggestion": "给出初步的最小可行性产品(MVP)应对策略或研发建议，越具体越好"
}}
"""
        
        try:
            # 使用传入的配置或默认值调用LLM
            # 根据provider设置默认model_name
            if not model_name:
                if provider == "gemini":
                    model_name = "gemini-2.5-pro"
                elif provider == "openai":
                    model_name = "gpt-4"
                elif provider == "deepseek":
                    model_name = "deepseek-chat"
                else:
                    model_name = ""

            raw_response = self.llm_service.call_llm(
                prompt=prompt,
                api_key=api_key,
                provider=provider,
                model_name=model_name,
                base_url=base_url if base_url else None
            )

            if self._looks_like_error_response(raw_response or ""):
                return self._build_analysis_fallback(
                    "模型调用返回错误信息，请检查配置后重试。",
                    merge_recommendations,
                )

            analysis_dict = self._parse_json_payload(raw_response)

        except Exception as e:
            print(f"[ReqAnalystAgent] LLM 解析异常: {e}")
            return self._build_analysis_fallback(
                "模型未返回有效的结构化分析结果，请重试。",
                merge_recommendations,
            )

        # 返回综合结果
        scenario_keywords = analysis_dict.get("scenario_keywords", [])
        if isinstance(scenario_keywords, str):
            scenario_keywords = [item.strip() for item in re.split(r"[、,，/\n]+", scenario_keywords) if item.strip()]
        elif not isinstance(scenario_keywords, list):
            scenario_keywords = []

        # 已知核心字段
        _KNOWN_FIELDS = {"core_problem", "current_product_behavior", "gap_analysis",
                         "root_cause", "module", "product_layer", "scenario_keywords", "mvp_suggestion"}
        base_result = {
            "core_problem": str(analysis_dict.get("core_problem", "") or analysis_dict.get("root_cause", "")).strip(),
            "current_product_behavior": str(analysis_dict.get("current_product_behavior", "") or "").strip(),
            "gap_analysis": str(analysis_dict.get("gap_analysis", "") or analysis_dict.get("root_cause", "")).strip(),
            "root_cause": analysis_dict.get("root_cause", ""),
            "module": analysis_dict.get("module", "待确认"),
            "product_layer": str(analysis_dict.get("product_layer", "待确认") or "待确认").strip(),
            "scenario_keywords": scenario_keywords[:8],
            "mvp_suggestion": analysis_dict.get(
                "mvp_suggestion",
                "本次自动分析未形成可用结论，请检查模型配置或稍后重试。",
            ),
            "merge_recommendation": merge_recommendations,
        }
        # 透传 LLM 返回的额外字段（如 value_score, alternative_solution 等扩展分析）
        extra = {k: v for k, v in analysis_dict.items() if k not in _KNOWN_FIELDS}
        base_result.update(extra)
        return base_result

    def run_task(self, task) -> Optional[Dict]:
        """AgentTask 入口：供流水线 Stage 4 (PRDDever) 调用，按主题对单条需求生成初稿。

        payload 字段：
          req_id     — 必须，目标需求 ID
          cluster_id — 可选，所属 cluster（写入 ai_analysis.theme_context）
        """
        payload = json.loads(task.payload_json or "{}")
        req_id = payload.get("req_id")
        cluster_id = payload.get("cluster_id")
        if not req_id:
            return {"error": "payload 缺少 req_id"}

        req = self.vector_store.get_requirement(req_id)
        if not req:
            return {"error": f"requirement {req_id} not found"}

        analysis = self.analyze(req)

        if cluster_id:
            analysis["theme_context"] = {"cluster_id": cluster_id}

        analysis["analyzed_at"] = datetime.utcnow().isoformat()

        # 持久化 ai_analysis 并翻转状态
        try:
            self.vector_store.update_requirement_field(req_id, {
                "ai_analysis": json.dumps(analysis, ensure_ascii=False, default=str),
                "status": "draft_review",
            })
        except Exception as e:
            return {"error": f"持久化失败: {e}", "req_id": req_id}

        return {"req_id": req_id, "cluster_id": cluster_id, "status": "draft_review"}
