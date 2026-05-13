"""
ReqpoolAdapter: Darwin M5 需求池分析模块适配器

封装竞品分析流水线的基因读写、评分、replay 和突变提议。

score_fast() 对分析输出做结构性检查：
  - 竞品命名率（是否提及 SAP/金蝶/泛微/致远）
  - 证据链存在性（URL / help_url）
  - 结论性判断（支持/不支持）
  - 技术证据（截图/API）
  - 时效标注（2025/2026 日期）
  - kb_coverage_rate（KB 文档覆盖率）
  - kb_freshness_rate（KB 文档时效性）
  - report_completeness（报告完整性）
  - report_depth_ratio（报告深度比）
  - report_cross_ref（报告交叉引用准确性）
  - prototype_element_completeness（原型元素完整性）
  - prototype_interaction_coverage（原型交互覆盖率）

run_pipeline() 对每条需求调用 _get_competitor_analysis_enhanced（或轻量降级）。
propose_mutations() 按最弱维度提议 prompt 突变。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from APP.backend.evolution_core.genome import Genome, load_genome
from APP.backend.evolution_core.constants import EVOLUTION_DIR

REGISTRY_PATH = "APP/backend/evolution_core/registry/reqpool.yaml"

# 竞品命名词（快速评分用）
VENDOR_NAMES = ["SAP", "金蝶", "泛微", "致远", "kingdee", "weaver", "seeyon"]
EVIDENCE_DOMAINS = [
    "help.sap.com",
    "vip.kingdee.com",
    "ecologyhelp.weaver.com.cn",
    "help.seeyon.com",
    "api.sap.com",
    "open.kingdee.com",
]


class ReqpoolAdapter:
    module_id = "reqpool"

    # ── 基因读写 ─────────────────────────────────────────────────────────────

    def read_genome(self) -> Genome:
        return load_genome(REGISTRY_PATH)

    def write_genome(self, genome: Genome) -> None:
        from APP.backend.evolution_core.ratchet import snapshot_genome
        snapshot_genome(genome)

    # ── 评分 ─────────────────────────────────────────────────────────────────

    def score_fast(self, eval_set_path: str | None = None) -> dict[str, float]:
        """快速评分（秒级）：对 eval set 中的分析输出做结构性检查。

        指标：
          vendor_naming_rate   — 分析中出现具体竞品名称的比例
          evidence_chain_rate  — 包含证据 URL 或 help_url 的比例
          conclusion_rate      — 包含结论性判断（支持/不支持）的比例
          tech_evidence_rate   — 包含截图/API 引用的比例
          freshness_rate       — 包含时效日期标注的比例
        """
        root = Path.cwd()
        es_path = eval_set_path or str(
            root / EVOLUTION_DIR / "reqpool" / "eval_sets" / "frozen.jsonl"
        )
        inputs = self.build_replay_inputs(es_path)
        if not inputs:
            return {
                "vendor_naming_rate": 0.0,
                "evidence_chain_rate": 0.0,
                "conclusion_rate": 0.0,
                "tech_evidence_rate": 0.0,
                "freshness_rate": 0.0,
                "eval_count": 0,
            }

        results = self.run_pipeline(inputs)
        n = len(results)
        vendor_naming = 0
        evidence_chain = 0
        conclusion = 0
        tech_evidence = 0
        freshness = 0

        for out in results:
            text = _extract_analysis_text(out)
            if any(v in text for v in VENDOR_NAMES):
                vendor_naming += 1
            if any(d in text for d in EVIDENCE_DOMAINS) or "http" in text:
                evidence_chain += 1
            if "支持" in text or "不支持" in text:
                conclusion += 1
            if "截图" in text or "API" in text or "api" in text.lower():
                tech_evidence += 1
            if re.search(r"202[56]-\d{2}", text):
                freshness += 1

        base_scores = {
            "vendor_naming_rate": vendor_naming / n,
            "evidence_chain_rate": evidence_chain / n,
            "conclusion_rate": conclusion / n,
            "tech_evidence_rate": tech_evidence / n,
            "freshness_rate": freshness / n,
            "eval_count": n,
        }

        # ── 探索质量维度（Phase 1+2）────────────────────────────────
        exploration_scores = self._score_exploration_quality()
        base_scores.update(exploration_scores)

        return base_scores

    # ── eval set 构建 ─────────────────────────────────────────────────────────

    def build_replay_inputs(self, eval_set_path: str) -> list[dict[str, Any]]:
        """从 eval set JSONL 构建输入列表（跳过注释行）。"""
        if not eval_set_path:
            return []
        path = Path(eval_set_path)
        if not path.is_absolute():
            path = Path.cwd() / eval_set_path
        if not path.exists():
            return []

        tickets: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    tickets.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return tickets

    # ── 流水线 ───────────────────────────────────────────────────────────────

    def run_pipeline(self, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对一批需求调用 _get_competitor_analysis_enhanced（graceful degrade）。"""
        outputs = []
        for req in inputs:
            out = _run_enhanced_analysis(req)
            outputs.append(out)
        return outputs

    # ── 探索质量评分 ─────────────────────────────────────────────────────────

    def _score_exploration_quality(self) -> dict[str, float]:
        """Compute 7 exploration quality dimensions across all KB/conclusion data.

        Returns partial scores that get merged into the main score_fast() result.
        """
        root = Path.cwd()
        scores: dict[str, float] = {
            "kb_coverage_rate": 0.0,
            "kb_freshness_rate": 0.0,
            "report_completeness": 0.0,
            "report_depth_ratio": 0.0,
            "report_cross_ref": 0.0,
            "prototype_element_completeness": 0.0,
            "prototype_interaction_coverage": 0.0,
        }

        # Discover exploration directories
        kb_base = root / "KB"
        conclusion_base = root / "conclusion" / "_local"
        if not kb_base.is_dir() or not conclusion_base.is_dir():
            return scores

        # Collect all vendor×feature KB dirs
        kb_dirs = [d for d in kb_base.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not kb_dirs:
            return scores

        # ── KB coverage & freshness ──────────────────────────────────────
        total_cells = 0
        covered_cells = 0
        fresh_cells = 0
        now = datetime.now()
        thirty_days_ago = now - timedelta(days=30)

        for kb_dir in kb_dirs:
            md_files = list(kb_dir.glob("*.md"))
            total_cells += 1  # each KB dir is one cell
            # Check if any doc > 1000 chars
            has_substantial = False
            has_fresh = False
            for md_file in md_files:
                try:
                    text = md_file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if len(text) > 1000:
                    has_substantial = True
                # Check freshness
                m = re.search(r"explored_at:\s*(\d{4}-\d{2}-\d{2})", text)
                if m:
                    try:
                        explored = datetime.strptime(m.group(1), "%Y-%m-%d")
                        if (now - explored) <= timedelta(days=30):
                            has_fresh = True
                    except ValueError:
                        pass
            if has_substantial:
                covered_cells += 1
            if has_fresh:
                fresh_cells += 1

        scores["kb_coverage_rate"] = round(covered_cells / max(total_cells, 1), 4)
        scores["kb_freshness_rate"] = round(fresh_cells / max(total_cells, 1), 4)

        # ── Report & prototype scoring (use first available exploration) ──
        # Find conclusion dirs that have findings.md
        for cdir in sorted(conclusion_base.iterdir()):
            if not cdir.is_dir():
                continue
            findings_path = cdir / "findings.md"
            if not findings_path.exists():
                continue

            screenshots_dir = cdir / "screenshots"
            kb_dir_match = kb_base / cdir.name

            # Score report quality
            try:
                from APP.backend.exploration_quality_scorer import ReportQualityScorer
                rqs = ReportQualityScorer()
                r_scores = rqs.score_report(
                    str(findings_path),
                    str(kb_dir_match) if kb_dir_match.is_dir() else "",
                    str(screenshots_dir) if screenshots_dir.is_dir() else "",
                )
                # Use first valid report scores (average if multiple)
                scores["report_completeness"] = r_scores.get("completeness", 0.0)
                scores["report_depth_ratio"] = r_scores.get("depth_ratio", 0.0)
                scores["report_cross_ref"] = r_scores.get("cross_ref_accuracy", 0.0)
            except Exception:
                pass

            # Score prototype quality (use first prototype subdir found)
            proto_base = cdir / "prototype"
            if proto_base.is_dir():
                proto_dirs = [d for d in proto_base.iterdir() if d.is_dir() and (d / "index.html").exists()]
                if proto_dirs:
                    try:
                        from APP.backend.exploration_prototype_scorer import PrototypeQualityScorer
                        pqs = PrototypeQualityScorer()
                        p_scores = pqs.score_prototype(
                            str(proto_dirs[0]),
                            str(findings_path),
                            str(screenshots_dir) if screenshots_dir.is_dir() else "",
                        )
                        scores["prototype_element_completeness"] = p_scores.get("element_completeness", 0.0)
                        scores["prototype_interaction_coverage"] = p_scores.get("interaction_coverage", 0.0)
                    except Exception:
                        pass

            break  # Use the first valid exploration

        return scores

    # ── 突变提议 ─────────────────────────────────────────────────────────────

    # Mapping from exploration quality dimensions to genome slots
    EXPLORATION_WEAKEST_TO_SLOT: dict[str, str | None] = {
        "report_completeness": "exploration_agent_prompt_template",
        "report_depth_ratio": "exploration_agent_prompt_template",
        "report_cross_ref": "report_quality_rubric",
        "prototype_element_completeness": "prototype_quality_rules",
        "prototype_interaction_coverage": "prototype_quality_rules",
        "kb_coverage_rate": None,   # don't auto-mutate, suggest to user
        "kb_freshness_rate": None,  # don't auto-mutate, suggest to user
    }

    def propose_mutations(
        self, weakest_dim: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """按最弱维度提议 prompt 突变。

        weakest_dim → slot mapping:
          vendor_naming_rate    → competitor_search_strategy
          evidence_chain_rate   → competitor_search_strategy
          conclusion_rate       → single_analysis_prompt
          tech_evidence_rate    → competitor_search_strategy
          freshness_rate        → single_analysis_prompt
          report_completeness   → exploration_agent_prompt_template
          report_depth_ratio    → exploration_agent_prompt_template
          report_cross_ref      → report_quality_rubric
          prototype_element_completeness  → prototype_quality_rules
          prototype_interaction_coverage  → prototype_quality_rules
          kb_coverage_rate      → None (suggest to user)
          kb_freshness_rate     → None (suggest to user)
        """
        genome: Genome | None = context.get("genome")
        mutations: list[dict[str, Any]] = []

        if weakest_dim in ("vendor_naming_rate", "evidence_chain_rate", "tech_evidence_rate"):
            slot = "competitor_search_strategy"
            current = genome.slots.get(slot, "") if genome else ""
            mutations.append({
                "slot_name": slot,
                "old_value_slice": (current or "")[:200],
                "new_value_slice": current,
                "rationale": (
                    f"{weakest_dim} is weakest: propose edits to competitor_search_strategy "
                    "to improve vendor-specific search terms and evidence URL collection"
                ),
                "mutation_type": "llm_edit_markdown",
            })

        elif weakest_dim in ("conclusion_rate", "freshness_rate"):
            slot = "single_analysis_prompt"
            current = genome.slots.get(slot, "") if genome else ""
            mutations.append({
                "slot_name": slot,
                "old_value_slice": (current or "")[:200],
                "new_value_slice": current,
                "rationale": (
                    f"{weakest_dim} is weakest: propose edits to single_analysis_prompt "
                    "to require explicit support/no-support conclusions and date annotations"
                ),
                "mutation_type": "llm_edit_markdown",
            })

        elif weakest_dim in self.EXPLORATION_WEAKEST_TO_SLOT:
            slot = self.EXPLORATION_WEAKEST_TO_SLOT[weakest_dim]
            if slot is None:
                # Non-auto-mutable dimension: suggest manual action
                mutations.append({
                    "slot_name": None,
                    "old_value_slice": "",
                    "new_value_slice": "",
                    "rationale": (
                        f"{weakest_dim} is weakest but cannot be auto-mutated. "
                        "Suggest re-running exploration or updating KB docs manually."
                    ),
                    "mutation_type": "user_action",
                })
            else:
                current = genome.slots.get(slot, "") if genome else ""
                mutations.append({
                    "slot_name": slot,
                    "old_value_slice": (current or "")[:200],
                    "new_value_slice": current,
                    "rationale": (
                        f"{weakest_dim} is weakest: propose edits to {slot} "
                        "to improve exploration output quality"
                    ),
                    "mutation_type": "llm_edit_markdown",
                })

        return mutations


# ── 内部辅助函数 ──────────────────────────────────────────────────────────────

def _run_enhanced_analysis(req: dict[str, Any]) -> dict[str, Any]:
    """单需求竞品分析增强包装。

    优先调用真实 ReqPoolDraftService._get_competitor_analysis_enhanced；
    不可用时降级为 CompetitorResearchService.research_with_cache。
    """
    try:
        import sys
        from pathlib import Path
        # Add APP/backend to path if needed
        backend_path = str(Path.cwd() / "APP" / "backend")
        if backend_path not in sys.path:
            sys.path.insert(0, backend_path)

        from competitor_account_manager import CompetitorAccountManager  # type: ignore
        from competitor_research_service import CompetitorResearchService  # type: ignore

        mgr = CompetitorAccountManager()
        svc = CompetitorResearchService()
        req_text = (req.get("title", "") + " " + req.get("description", "")).strip()
        feature_ids = mgr.match_requirement_to_features(req_text) or ["workflow_approval"]

        evidence = svc.research_with_cache(req, feature_ids)

        matrices: list[dict[str, Any]] = []
        root = Path.cwd()
        for fid in feature_ids:
            mp = root / "data_cache" / "competitor_validation" / "feature_matrix" / f"{fid}.json"
            if mp.exists():
                try:
                    import json as _json
                    matrices.append(_json.loads(mp.read_text(encoding="utf-8")))
                except Exception:
                    pass

        return {
            "req_id": req.get("id") or req.get("req_id", ""),
            "evidence": evidence,
            "feature_matrices": matrices,
            "matched_features": feature_ids,
        }
    except Exception:
        return {
            "req_id": req.get("id") or req.get("req_id", ""),
            "evidence": [],
            "feature_matrices": [],
            "matched_features": [],
        }


def _extract_analysis_text(output: dict[str, Any]) -> str:
    """Flatten an analysis output dict into a single text string for scoring."""
    parts: list[str] = []

    # evidence snippets
    for ev in output.get("evidence", []) or []:
        parts.append(ev.get("content_snippet", "") or ev.get("title", ""))
        parts.append(ev.get("url", ""))
        parts.append(ev.get("collected_at", ""))

    # feature matrices
    for matrix in output.get("feature_matrices", []) or []:
        parts.append(matrix.get("last_updated", ""))
        for vendor_id, vdata in (matrix.get("vendors", {}) or {}).items():
            parts.append(vendor_id)
            parts.append(vdata.get("support_status", ""))
            parts.append(vdata.get("key_differences", ""))
            parts.append(vdata.get("help_url", ""))
            parts.append(vdata.get("last_verified", ""))
            parts.extend(vdata.get("api_endpoints", []) or [])

    # competitor_comparison (legacy bundle format)
    comp = output.get("competitor_comparison") or {}
    for vendor in comp.get("vendors", []) or []:
        parts.append(vendor.get("vendor", ""))
        for c in vendor.get("citations", []) or []:
            parts.append(c.get("url", ""))
            parts.append(c.get("title", ""))

    return " ".join(str(p) for p in parts if p)
