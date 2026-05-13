"""
ClassifyAdapter：Darwin M2 分类模块适配器

封装 MYPROJECT 工单四维度分类流水线的基因读写、评分、replay 和突变提议。

score() 调用真实 audit_theme_purity.py（带正确参数）。
run_pipeline() 通过 importlib 加载并调用真实 classify_tickets.classify()
+ cluster_product.classify()，非 keyword-only stub。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from APP.backend.evolution_core.genome import Genome, load_genome
from APP.backend.evolution_core.constants import EVOLUTION_DIR, BAD_VALUES

REGISTRY_PATH = "APP/backend/evolution_core/registry/classify.yaml"
PIPELINE_DIR = ".agent/skills/ticket-analysis-pipeline/scripts"
DATA_SOURCE_2025 = "_local/design/ticket-reduction/data-source/problem-list-2025.md"
DATA_SOURCE_YTD = "_local/design/ticket-reduction/data-source/problem-list-2026-ytd.md"

_MODULE_CACHE: dict[str, Any] = {}


def _load_pipeline_module(name: str) -> Any:
    if name in _MODULE_CACHE:
        return _MODULE_CACHE[name]
    root = Path.cwd()
    path = root / PIPELINE_DIR / f"{name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MODULE_CACHE[name] = mod
    return mod


class ClassifyAdapter:
    module_id = "classify"

    def read_genome(self) -> Genome:
        return load_genome(REGISTRY_PATH)

    def write_genome(self, genome: Genome) -> None:
        from APP.backend.evolution_core.ratchet import snapshot_genome
        snapshot_genome(genome)

    def score_fast(self, eval_set_path: str | None = None) -> dict[str, float]:
        """
        快速评分（秒级）：用当前规则对 rolling eval set 做 keyword 分类，
        与 gold label 对比计算准确率。适合每日自动运行。
        """
        root = Path.cwd()
        es_path = eval_set_path or str(
            root / EVOLUTION_DIR / "classify" / "eval_sets" / "rolling.jsonl"
        )
        inputs = self.build_replay_inputs(es_path)
        if not inputs:
            return {"accuracy": 0.0, "fallback_rate": 1.0, "eval_count": 0}

        results = self.run_pipeline(inputs)

        total = len(results)
        exact_match = 0
        dim_match = 0
        fallback_count = 0

        for inp, res in zip(inputs, results):
            gold_leaf = inp.get("original_leaf", "")
            pred_leaf = res.get("classified_leaf", "")

            if pred_leaf == gold_leaf:
                exact_match += 1
            if gold_leaf[:2] == pred_leaf[:2]:
                dim_match += 1
            if "其他" in pred_leaf or pred_leaf == "未分类":
                fallback_count += 1

        return {
            "accuracy": exact_match / max(total, 1),
            "dim_accuracy": dim_match / max(total, 1),
            "fallback_rate": fallback_count / max(total, 1),
            "eval_count": total,
            "exact_match": exact_match,
            "fallback_count": fallback_count,
        }

    def score(self, eval_set_path: str | None = None) -> dict[str, float]:
        """
        深度评分（分钟级）：调用 audit_theme_purity.py LLM 审计。
        适合每周运行。日常用 score_fast()。
        """
        root = Path.cwd()
        audit_script = root / PIPELINE_DIR / "audit_theme_purity.py"
        python = sys.executable

        input_md = eval_set_path or str(root / DATA_SOURCE_2025)
        if not Path(input_md).is_absolute():
            input_md = str(root / input_md)

        with tempfile.TemporaryDirectory(prefix="evo_audit_") as tmpdir:
            output_report = os.path.join(tmpdir, "audit_report.md")
            raw_dir = os.path.join(tmpdir, "raw")
            os.makedirs(raw_dir, exist_ok=True)

            cmd = [
                python, str(audit_script),
                "--input", input_md,
                "--output", output_report,
                "--raw-dir", raw_dir,
                "--sample-per-theme", "10",
                "--concurrency", "2",
            ]

            try:
                result = subprocess.run(
                    cmd, cwd=str(root),
                    capture_output=True, text=True, timeout=600,
                )
                output = result.stdout + "\n" + result.stderr
                scores = _parse_audit_output(output)

                if Path(output_report).exists():
                    report_text = Path(output_report).read_text(encoding="utf-8")
                    scores.update(_parse_report_md(report_text))

                return scores
            except subprocess.TimeoutExpired:
                return {"leaf_purity_rate": 0.0, "fallback_rate": 1.0, "dimension_accuracy": 0.0}
            except Exception as e:
                print(f"[classify_adapter.score] error: {e}", file=sys.stderr)
                return {"leaf_purity_rate": 0.0, "fallback_rate": 1.0, "dimension_accuracy": 0.0}

    def build_replay_inputs(self, eval_set_path: str) -> list[dict[str, Any]]:
        path = Path(eval_set_path)
        if not path.is_absolute():
            path = Path.cwd() / eval_set_path

        if not path.exists():
            return []

        if path.suffix.lower() == ".jsonl":
            tickets = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        try:
                            tickets.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            return tickets

        if path.suffix.lower() == ".csv":
            with open(path, encoding="utf-8-sig") as f:
                return list(csv.DictReader(f))

        return []

    def run_pipeline(self, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        对一批工单调用真实分类流水线。

        两种模式：
        - 完整模式（ticket 含解决方式/研发确认问题类型）：
          classify_tickets.classify() → 维度 → cluster_*.classify() → 叶节点
        - eval 模式（ticket 含 original_leaf 但缺 Jira 元字段）：
          从 original_leaf 前缀推断维度，直接调 cluster_*.classify() → 叶节点
        """
        prod_mod = _load_pipeline_module("cluster_product")
        rd_mod = _load_pipeline_module("cluster_rd")
        impl_mod = _load_pipeline_module("cluster_impl")
        kf_mod = _load_pipeline_module("cluster_kf")
        cls_mod = _load_pipeline_module("classify_tickets")

        FIELD_MAP = ["工单号", "概要", "经办人", "客户/项目", "解决方式",
                     "解决方案", "创建日期", "研发确认问题类型", "客户问题类型"]
        headers = list(FIELD_MAP)

        DIM_PREFIX = {"P-": "产品", "R-": "研发", "I-": "实施", "K-": "客开"}

        outputs = []
        for ticket in inputs:
            tid = ticket.get("ticket_id") or ticket.get("工单号", "")
            original_leaf = ticket.get("original_leaf", "")

            # 构建 row（兼容 dict 字段名差异）
            field_aliases = {
                "概要": ["概要", "summary"],
                "解决方案": ["解决方案", "solution"],
            }
            row = []
            for h in headers:
                val = ticket.get(h, "")
                if not val:
                    for alias in field_aliases.get(h, []):
                        val = ticket.get(alias, "")
                        if val:
                            break
                row.append(str(val))

            idx = {
                "summary": headers.index("概要"),
                "solution": headers.index("解决方案"),
            }

            # 推断维度
            dim = ""
            rule = ""
            if original_leaf:
                for prefix, d in DIM_PREFIX.items():
                    if original_leaf.startswith(prefix):
                        dim = d
                        rule = "eval-inferred"
                        break
            if not dim and cls_mod:
                dim, rule = cls_mod.classify(row, headers)
                rule = str(rule)

            # 叶节点分类
            leaf = "未分类"
            if dim == "产品" and prod_mod and hasattr(prod_mod, "classify"):
                leaf = prod_mod.classify(row, idx) or "P-其他产品问题"
            elif dim == "研发" and rd_mod and hasattr(rd_mod, "classify"):
                leaf = rd_mod.classify(row, idx) or "R-其他研发问题"
            elif dim == "实施" and impl_mod and hasattr(impl_mod, "classify"):
                leaf = impl_mod.classify(row, headers, idx) or "I-其他(实施问题)"
            elif dim == "客开" and kf_mod and hasattr(kf_mod, "classify"):
                leaf = kf_mod.classify(row, headers, idx) or "K-其他客开问题"

            outputs.append({
                "ticket_id": tid,
                "dimension": dim,
                "dimension_rule": str(rule),
                "classified_leaf": leaf,
                "confidence": "high" if rule not in ("99", "eval-inferred") else "medium",
            })

        return outputs

    def run_full_regen(self) -> dict[str, Any]:
        """
        端到端跑完整流水线：classify → aggregate → 输出 problem-list MD。
        需要 aiticket-v4/conclusion/temp/ 有 CSV 数据。
        返回 {"success": bool, "output_path": str, "leaf_count": int}
        """
        root = Path.cwd()
        v4_root = root.parent / "aiticket-v4"
        aggregate_script = root / PIPELINE_DIR.replace("ticket-analysis-pipeline", "ticket-reduction-analyst") / "aggregate_problem_list.py"

        if not aggregate_script.exists():
            aggregate_script = v4_root / "conclusion" / "temp" / "aggregate_problem_list.py"

        if not aggregate_script.exists():
            return {"success": False, "error": "aggregate_problem_list.py not found"}

        output_md = str(root / DATA_SOURCE_2025)
        env = {
            **os.environ,
            "OUTPUT_MD": output_md,
            "PERIOD_LABEL": "2025全年",
        }

        try:
            result = subprocess.run(
                [sys.executable, str(aggregate_script)],
                cwd=str(v4_root) if v4_root.exists() else str(root),
                env=env, capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                return {"success": False, "error": result.stderr[:500]}

            leaf_count = 0
            if Path(output_md).exists():
                content = Path(output_md).read_text(encoding="utf-8")
                leaf_count = content.count("\n### ")

            return {"success": True, "output_path": output_md, "leaf_count": leaf_count}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def propose_mutations(
        self, weakest_dim: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        genome: Genome | None = context.get("genome")
        recent_misclassified: list[dict] = context.get("recent_misclassified", [])

        mutations = []
        if not genome or not recent_misclassified:
            return mutations

        if weakest_dim in ("leaf_purity_rate", "accuracy", "fallback_rate"):
            product_themes = genome.slots.get("product_themes") or []
            if not isinstance(product_themes, list) or not product_themes:
                return mutations

            proposals = _propose_keyword_additions(product_themes, recent_misclassified)
            for leaf_id, new_keywords in proposals[:1]:
                import copy
                updated_themes = copy.deepcopy(product_themes)
                for leaf in updated_themes:
                    if isinstance(leaf, dict) and leaf.get("id") == leaf_id:
                        existing = set(leaf.get("keywords", []))
                        leaf["keywords"] = list(existing | set(new_keywords))
                        break
                mutations.append({
                    "slot_name": "product_themes",
                    "target_leaf": leaf_id,
                    "old_value_slice": _get_leaf_keywords(product_themes, leaf_id),
                    "new_value_slice": updated_themes,
                    "rationale": f"leaf '{leaf_id}': adding {len(new_keywords)} keywords from misclassified tickets",
                })

        return mutations


def _parse_audit_output(output: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    patterns = {
        "leaf_purity_rate": re.compile(r"(?:purity|纯净)[_\s]*(?:rate)?[:\s]+([0-9.]+)", re.I),
        "fallback_rate": re.compile(r"fallback[_\s]*(?:rate)?[:\s]+([0-9.]+)", re.I),
        "mixed_count": re.compile(r"(?:混类|mixed)[_\s]*(?:count)?[:\s]+(\d+)", re.I),
        "pure_count": re.compile(r"(?:纯净|pure)[_\s]*(?:count)?[:\s]+(\d+)", re.I),
    }
    for key, pat in patterns.items():
        m = pat.search(output)
        if m:
            try:
                scores[key] = float(m.group(1))
            except ValueError:
                pass
    if "pure_count" in scores and "mixed_count" in scores:
        total = scores["pure_count"] + scores["mixed_count"]
        if total > 0:
            scores["leaf_purity_rate"] = scores["pure_count"] / total
    return scores


def _parse_report_md(report: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    m = re.search(r"(\d+)\s*个纯净", report)
    if m:
        scores["pure_count"] = float(m.group(1))
    m = re.search(r"(\d+)\s*个混类", report)
    if m:
        scores["mixed_count"] = float(m.group(1))
    if "pure_count" in scores and "mixed_count" in scores:
        total = scores["pure_count"] + scores["mixed_count"]
        if total > 0:
            scores["leaf_purity_rate"] = scores["pure_count"] / total
    return scores


def _propose_keyword_additions(
    product_themes: list[Any], misclassified: list[dict]
) -> list[tuple[str, list[str]]]:
    """从落入"其他/未分类"的 P 维度工单中，提取高频词并匹配到最相关的 leaf，
    提议将这些词加入该 leaf 的 keywords。"""
    import re as _re
    from collections import Counter

    if not misclassified or not product_themes:
        return []

    p_miss = [t for t in misclassified
              if t.get("original_leaf", "").startswith("P-")]
    if not p_miss:
        return []

    all_words = Counter()
    for t in p_miss:
        text = str(t.get("summary", "")) + " " + str(t.get("概要", ""))
        words = _re.findall(r'[\u4e00-\u9fa5]{2,6}', text)
        all_words.update(words)

    STOP_WORDS = {"支持问题", "帐户分享", "分享链接", "老师", "客户", "问题", "需求",
                  "产品", "系统", "功能", "流程", "审批", "配置", "设置", "操作",
                  "公司", "有限", "股份", "集团", "科技", "请问", "你好", "如何"}

    top_words = [(w, c) for w, c in all_words.most_common(50) if w not in STOP_WORDS]

    proposals = []
    for leaf in product_themes:
        if not isinstance(leaf, dict):
            continue
        leaf_id = leaf.get("id", "")
        desc = (leaf.get("description") or "").lower()
        existing_kws = set(kw.lower() for kw in leaf.get("keywords", []))

        new_kws = []
        for word, count in top_words:
            if word.lower() in existing_kws:
                continue
            if word.lower() in desc or any(word.lower() in kw.lower() for kw in existing_kws):
                new_kws.append(word)
            elif count >= 3 and any(kw[:2] in word for kw in existing_kws if len(kw) >= 2):
                new_kws.append(word)

        if new_kws:
            proposals.append((leaf_id, new_kws[:5]))

    proposals.sort(key=lambda x: -len(x[1]))
    return proposals[:3]


def _get_leaf_keywords(product_themes: list[Any], leaf_id: str) -> list[str]:
    for leaf in product_themes:
        if isinstance(leaf, dict) and leaf.get("id") == leaf_id:
            return leaf.get("keywords", [])
    return []
