"""
PM 原始需求分诊服务
- 调用 ReqAnalystAgent 对需求进行 AI 分析（含价值评分、变相方案、分诊建议）
- 分析结果缓存到 data_cache/pm_triage/{aid}.json
- 提供 summarize_board() 聚合看板统计
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BASE_DIR = Path(os.environ.get("DEMO_RUNTIME_DIR") or Path(__file__).parent.parent)
_TRIAGE_CACHE_DIR = _BASE_DIR / "data_cache" / "pm_triage"
_TRIAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_TRIAGE_QUESTIONS = """

【额外分析要求（JSON 中增加以下 4 个字段）】
1. value_score（整数 0-100）：综合评估该需求的业务价值。
   评分维度自行判断，示例参考：影响用户量（是否通用场景）、业务阻塞程度（有无 workaround）、
   提出方背景（内部建议 vs 客户实际痛点）、替代方案多寡（现有产品能否绕行）。
   请在 value_reasons 字段（字符串列表）中列出评分主要依据（2-4 条）。

2. alternative_solution（字符串）：若用户诉求可通过现有产品能力以变相/迂回方式满足，
   请填写具体操作路径（简短，1-3 句）；若无法变相实现则填空字符串 ""。

3. triage_recommendation（枚举字符串）：
   - "auto_reject"：价值低 AND 无变相方案，可直接拒绝，不值得人工处理
   - "auto_alternative"：有可行的变相方案，可自动回复告知用户绕行方法
   - "manual"：需要人工判断（价值较高，或情况复杂，或无法确定）

4. triage_reason（字符串）：解释你为何给出该分诊建议，一句话即可。

请将以上 4 个字段和原有字段一起输出到同一个 JSON 对象中，不要嵌套。
"""


def _triage_cache_path(aid: str) -> Path:
    return _TRIAGE_CACHE_DIR / f"{aid}.json"


def _load_cached(aid: str) -> Optional[Dict]:
    p = _triage_cache_path(aid)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save_cache(aid: str, data: Dict) -> None:
    with open(_triage_cache_path(aid), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def _load_llm_config() -> Dict[str, str]:
    """读取 llm_config.json，返回当前 provider 的配置"""
    config_path = _BASE_DIR / "llm_config.json"
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        provider = cfg.get("last_provider", "")
        prov_cfg = cfg.get(provider, {})
        return {
            "provider": provider,
            "apiKey": prov_cfg.get("api_key") or prov_cfg.get("apiKey", ""),
            "modelName": prov_cfg.get("model_name") or prov_cfg.get("modelName", ""),
            "baseUrl": prov_cfg.get("base_url") or prov_cfg.get("baseUrl", ""),
        }
    except Exception:
        return {}


class PMTriageService:
    """
    PM 原始需求分诊服务。
    不绑定 entityType，适用于任何通过 PMModuleService 获取的需求记录。
    """

    def __init__(self):
        # 懒加载 ReqAnalystAgent（依赖 vector_store，需要在 FastAPI 上下文中获取）
        self._agent = None
        self._agent_lock = threading.Lock()

        # 从 pm_config.yaml 读取分诊规则
        self._rules = self._load_triage_rules()

    def _load_triage_rules(self) -> Dict[str, Any]:
        try:
            import yaml
            cfg_path = _BASE_DIR / "config" / "pm_config.yaml"
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f).get("pm_system", {})
            return cfg.get("triage_rules", {})
        except Exception:
            return {}

    def _get_agent(self):
        """懒加载 ReqAnalystAgent，thread-safe"""
        if self._agent is None:
            with self._agent_lock:
                if self._agent is None:
                    try:
                        from agents.req_analyst_agent import ReqAnalystAgent
                        from vector_store import VectorStore
                        vs = VectorStore()
                        self._agent = ReqAnalystAgent(vs)
                    except Exception as e:
                        print(f"[PMTriageService] ReqAnalystAgent 初始化失败: {e}")
                        self._agent = None
        return self._agent

    # ------------------------------------------------------------------
    # 核心分析方法
    # ------------------------------------------------------------------

    def analyze_demand(
        self,
        demand: Dict[str, Any],
        llm_config: Optional[Dict] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        对单条需求进行 AI 分析（含分诊评分）。
        - force=True：忽略缓存，重新分析
        - 结果缓存到 data_cache/pm_triage/{aid}.json
        """
        aid = demand.get("aid", "")
        if not force:
            cached = _load_cached(aid)
            if cached and cached.get("analyzed_at"):
                return {"success": True, "from_cache": True, "analysis": cached}

        agent = self._get_agent()
        if not agent:
            return {"success": False, "message": "ReqAnalystAgent 不可用，请检查服务配置"}

        if not llm_config:
            llm_config = _load_llm_config()

        # 拼接原始描述 + 额外分诊问题
        title = demand.get("title", "")
        description = demand.get("description") or ""
        source = demand.get("source", "")
        assignee_name = demand.get("assignee_name", "")

        req_dict = {
            "req_id": f"PMDEMAND-{aid}",
            "title": title,
            "description": (
                f"【来源】{source}\n【经办人】{assignee_name}\n\n{description}"
                + _TRIAGE_QUESTIONS
            ),
        }

        try:
            raw = agent.analyze(req_dict, llm_config)
        except Exception as e:
            return {"success": False, "message": f"LLM 分析失败: {e}"}

        # 提取新增字段（Agent 会把它们放在根层）
        analysis = {
            **raw,
            "aid": aid,
            "code": demand.get("code", ""),
            "title": title,
            "status": demand.get("status", ""),
            "value_score": self._safe_int(raw.get("value_score"), 50),
            "value_reasons": raw.get("value_reasons") or [],
            "alternative_solution": raw.get("alternative_solution") or "",
            "triage_recommendation": raw.get("triage_recommendation") or "manual",
            "triage_reason": raw.get("triage_reason") or "",
            "analyzed_at": datetime.now().isoformat(),
        }

        _save_cache(aid, analysis)
        return {"success": True, "from_cache": False, "analysis": analysis}

    @staticmethod
    def _safe_int(val, default: int = 50) -> int:
        try:
            return max(0, min(100, int(val)))
        except (TypeError, ValueError):
            return default

    def get_cached_analysis(self, aid: str) -> Optional[Dict]:
        """获取已缓存的分析结果（供外部调用）"""
        return _load_cached(aid)

    # alias for backward compat
    _load_cached_public = get_cached_analysis

    def decide(self, demand: Dict, analysis: Dict) -> str:
        """
        将 Agent 的 triage_recommendation 映射到最终分诊决策。
        加入阈值规则兜底（防止 LLM 给出不合理的建议）。
        """
        rec = analysis.get("triage_recommendation", "manual")
        score = self._safe_int(analysis.get("value_score"), 50)
        alt = bool(analysis.get("alternative_solution", "").strip())

        auto_reject_lt = self._rules.get("auto_reject_score_lt", 40)
        auto_alt_lt = self._rules.get("auto_alternative_score_lt", 70)

        # Agent 建议 auto_reject：再检查分数，防止误杀高价值需求
        if rec == "auto_reject":
            return "auto_reject" if score < auto_alt_lt else "manual"

        # Agent 建议 auto_alternative：确认有可行方案
        if rec == "auto_alternative":
            return "auto_alternative" if alt else "manual"

        # 兜底规则：极低价值 + 无方案 → 仍然建议拒绝（即使 Agent 不确定）
        if score < auto_reject_lt and not alt:
            return "auto_reject"

        return "manual"

    # ------------------------------------------------------------------
    # 批量分析
    # ------------------------------------------------------------------

    def batch_analyze(
        self,
        demands: List[Dict],
        llm_config: Optional[Dict] = None,
        concurrency: int = 3,
        force: bool = False,
        on_progress=None,
    ) -> Dict[str, Any]:
        """
        并发批量分析需求。
        - concurrency: 最大并发数（不要超过 5，避免 rate limit）
        - on_progress: 可选回调 fn(done, total, aid)
        - 返回 {success_count, error_count, results: {aid: analysis}}
        """
        if not llm_config:
            llm_config = _load_llm_config()

        results: Dict[str, Any] = {}
        errors: List[Dict] = []
        total = len(demands)
        done = 0

        def analyze_one(d: Dict) -> Tuple[str, Dict]:
            aid = d.get("aid", "")
            result = self.analyze_demand(d, llm_config=llm_config, force=force)
            return aid, result

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(analyze_one, d): d for d in demands}
            for future in as_completed(futures):
                aid, result = future.result()
                done += 1
                if result.get("success"):
                    results[aid] = result["analysis"]
                    if on_progress:
                        on_progress(done, total, aid)
                else:
                    errors.append({"aid": aid, "error": result.get("message")})
                    if on_progress:
                        on_progress(done, total, aid)

        return {
            "success_count": len(results),
            "error_count": len(errors),
            "total": total,
            "results": results,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # 看板总结
    # ------------------------------------------------------------------

    def summarize_board(self, demands: Optional[List[Dict]] = None,
                        allowed_aids: Optional[set] = None) -> Dict[str, Any]:
        """
        聚合所有已分析需求，生成看板总结。
        - demands: 可选，保留兼容参数（未使用）
        - allowed_aids: 若传入，只统计 aid 在集合中的缓存（用于应用/服务过滤）
        """
        # 从 cache 文件读取已分析结果（支持 allowed_aids 过滤）
        all_analyses = []
        for p in _TRIAGE_CACHE_DIR.glob("*.json"):
            aid = p.stem  # 文件名即 aid
            if allowed_aids is not None and aid not in allowed_aids:
                continue  # 跳过不在过滤集合内的缓存
            try:
                with open(p) as f:
                    all_analyses.append(json.load(f))
            except Exception:
                continue

        if not all_analyses:
            return {
                "total_analyzed": 0,
                "module_distribution": {},
                "theme_distribution": {},
                "value_buckets": {"high": 0, "mid": 0, "low": 0},
                "triage_summary": {"auto_reject": 0, "auto_alternative": 0, "manual": 0, "pending": 0},
                "top_core_demands": [],
                "generated_at": datetime.now().isoformat(),
            }

        module_dist: Dict[str, int] = {}
        theme_dist: Dict[str, int] = {}
        # 每个 theme 收集前 N 个 core_problem，含 aid/code 供前端生成 Jira 链接
        theme_problems: Dict[str, List[Dict]] = {}
        value_buckets = {"high": 0, "mid": 0, "low": 0}
        triage_summary = {"auto_reject": 0, "auto_alternative": 0, "manual": 0, "pending": 0}
        core_demands: List[Dict] = []

        for a in all_analyses:
            # 模块分布
            mod = a.get("module") or "其他"
            module_dist[mod] = module_dist.get(mod, 0) + 1

            # 主题（场景关键词 → 取第一个）
            kws = a.get("scenario_keywords", [])
            theme = kws[0] if kws else "其他"
            theme_dist[theme] = theme_dist.get(theme, 0) + 1

            # 收集该 theme 下的核心问题（含 aid/code，供前端渲染 Jira 链接）
            cp = (a.get("core_problem") or "").strip()
            if cp:
                theme_problems.setdefault(theme, []).append({
                    "text": cp[:50],
                    "aid": a.get("aid", ""),
                    "code": a.get("code", ""),
                })

            # 价值分桶
            score = self._safe_int(a.get("value_score"), 50)
            if score >= 70:
                value_buckets["high"] += 1
            elif score >= 40:
                value_buckets["mid"] += 1
            else:
                value_buckets["low"] += 1

            # 分诊统计
            rec = self.decide({"aid": a.get("aid", "")}, a)
            triage_summary[rec] = triage_summary.get(rec, 0) + 1

            # 核心诉求（保留 value_score 较高的）
            if score >= 60 and a.get("core_problem"):
                core_demands.append({
                    "aid": a.get("aid"),
                    "code": a.get("code"),
                    "demand": a.get("core_problem"),
                    "value_score": score,
                    "module": mod,
                })

        # 取 top 10（高价值+高重复）
        core_demands.sort(key=lambda x: x["value_score"], reverse=True)
        top_core = core_demands[:10]

        # 每个 theme 的代表问题（去重 + 最多 3 条，带 aid/code 供前端生成 Jira 链接）
        import re as _re
        theme_core_problems: Dict[str, List[Dict]] = {}
        for theme, problems in theme_problems.items():
            seen_texts = []
            result = []
            for p in problems:
                raw_text = p["text"]
                snippet = raw_text[:45]
                m = _re.search(r'[，。；,.;!?！？]', snippet)
                if m and m.start() >= 10:
                    short = snippet[:m.start()].strip()
                else:
                    short = snippet.strip("，。,.；;!?！？")
                if len(short) > 40:
                    short = short[:40] + "…"
                if short and short not in seen_texts:
                    seen_texts.append(short)
                    result.append({"text": short, "aid": p["aid"], "code": p["code"]})
                if len(result) >= 3:
                    break
            if result:
                theme_core_problems[theme] = result

        return {
            "total_analyzed": len(all_analyses),
            "module_distribution": dict(sorted(module_dist.items(), key=lambda x: -x[1])),
            "theme_distribution": dict(sorted(theme_dist.items(), key=lambda x: -x[1])[:10]),
            "theme_core_problems": theme_core_problems,
            "value_buckets": value_buckets,
            "triage_summary": triage_summary,
            "top_core_demands": top_core,
            "generated_at": datetime.now().isoformat(),
        }


    def generate_summary_markdown(self, summary: Dict, label: str = "") -> str:
        """将 summarize_board 的结果生成 Markdown 文本，用于导出到 conclusion/exports/"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# 原始需求智能分析总结",
            f"",
            f"> 生成时间：{now}  过滤条件：{label or '全部'}",
            f"",
            f"---",
            f"",
            f"## 概览统计",
            f"",
            f"| 指标 | 数量 |",
            f"|------|------|",
            f"| 已分析总数 | {summary.get('total_analyzed', 0)} |",
        ]
        bk = summary.get("value_buckets", {})
        ts = summary.get("triage_summary", {})
        lines += [
            f"| 高价值（≥70分） | {bk.get('high', 0)} |",
            f"| 中等（40-69分） | {bk.get('mid', 0)} |",
            f"| 低价值（<40分） | {bk.get('low', 0)} |",
            f"| 建议拒绝 | {ts.get('auto_reject', 0)} |",
            f"| 可变相实现 | {ts.get('auto_alternative', 0)} |",
            f"| 待人工处理 | {ts.get('manual', 0)} |",
            f"| 待分诊 | {ts.get('pending', 0)} |",
            f"",
            f"---",
            f"",
            f"## 高价值核心需求 TOP10",
            f"",
        ]
        tops = summary.get("top_core_demands", [])
        if tops:
            for i, d in enumerate(tops[:10], 1):
                code = f"[{d['code']}]" if d.get("code") else ""
                lines.append(f"{i}. {code} {d.get('demand', '')} （{d.get('value_score', 0)}分）")
        else:
            lines.append("*暂无高价值需求数据*")

        lines += [
            f"",
            f"---",
            f"",
            f"## 主题分类分析",
            f"",
        ]
        theme_dist = summary.get("theme_distribution", {})
        theme_cp = summary.get("theme_core_problems", {})
        total_analyzed = summary.get("total_analyzed", 1) or 1
        for theme, cnt in list(theme_dist.items())[:10]:
            pct = round(cnt / total_analyzed * 100)
            lines.append(f"### {theme}（{cnt}条，{pct}%）")
            problems = theme_cp.get(theme, [])
            if problems:
                parts = []
                for p in problems[:3]:
                    text = p["text"] if isinstance(p, dict) else p
                    code = p.get("code", "") if isinstance(p, dict) else ""
                    entry = text + (f" [{code}]" if code else "")
                    parts.append(entry)
                lines.append(f"主要问题：{' / '.join(parts)}")
            lines.append("")

        return "\n".join(lines)


# ------------------------------------------------------------------
# 单例
# ------------------------------------------------------------------
_triage_service_instance: Optional[PMTriageService] = None
_triage_lock = threading.Lock()


def get_pm_triage_service() -> PMTriageService:
    global _triage_service_instance
    if _triage_service_instance is None:
        with _triage_lock:
            if _triage_service_instance is None:
                _triage_service_instance = PMTriageService()
    return _triage_service_instance
