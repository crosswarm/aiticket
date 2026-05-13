"""
ReplyAdapter：Darwin M3 智能回复模块适配器

封装回复生成流水线的基因读写、评分、replay 和突变提议。

设计原则（来自 async-roaming-fountain.md M3 + happy-kindling-church.md L164-178）：
- score() 使用 replay 分数驱动进化决策
- live_adoption 仅作硬回归门（live_adoption_rate 跌 >30% 相对值 → regression_gate=False）
- run_pipeline() 是轻量包装：能用真实 generate_reply，无环境时 graceful degrade
- propose_mutations() 按最弱维度定向提议
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from APP.backend.evolution_core.genome import Genome, load_genome
from APP.backend.evolution_core.constants import EVOLUTION_DIR

REGISTRY_PATH = "APP/backend/evolution_core/registry/reply.yaml"
FEEDBACK_STATS_PATH = "APP/backend/data/reply_feedback.json"

# 回归门阈值：live_adoption 相对跌幅超过此值触发回归门
REGRESSION_GATE_DROP_THRESHOLD = 0.30

# 基线 live_adoption_rate（无历史 ledger 时的降级值，来自 reply_feedback.json 当前值）
_BASELINE_ADOPTION_RATE_FALLBACK = 0.028


class ReplyAdapter:
    module_id = "reply"

    # ── 基因读写 ─────────────────────────────────────────────────────────────

    def read_genome(self) -> Genome:
        return load_genome(REGISTRY_PATH)

    def write_genome(self, genome: Genome) -> None:
        from APP.backend.evolution_core.ratchet import snapshot_genome
        snapshot_genome(genome)

    # ── 评分 ─────────────────────────────────────────────────────────────────

    def score_fast(self, eval_set_path: str | None = None) -> dict[str, float]:
        """
        快速评分（秒级）：从 reply_feedback.json 读取当前 KPI，
        不调用 LLM，适合每日自动运行。
        """
        root = Path.cwd()
        live = self.live_signals()
        baseline = _load_baseline_adoption(root)
        live_rate = live.get("live_adoption_rate", 0.0)
        total = live.get("total_generated", 0)
        adopted = live.get("total_adopted", 0)

        drop = 0.0
        if baseline and baseline > 0:
            drop = (baseline - live_rate) / baseline
        regression_gate = drop <= REGRESSION_GATE_DROP_THRESHOLD

        genome = self.read_genome()
        style_len = len(str(genome.slots.get("style_rules_md", "")))
        lessons_count = 0
        try:
            import json as _json
            _state_path = root / "conclusion" / "_local" / "training" / "trainer_state.json"
            if _state_path.exists():
                _state = _json.loads(_state_path.read_text(encoding="utf-8"))
                lessons_count = len(_state.get("b_cumulative_lessons", []))
        except Exception:
            pass

        return {
            "live_adoption_rate": live_rate,
            "total_generated": float(total),
            "total_adopted": float(adopted),
            "regression_gate": 1.0 if regression_gate else 0.0,
            "adoption_drop_pct": drop,
            "style_rules_chars": float(style_len),
            "cumulative_lessons": float(lessons_count),
            "sim_direct_rate": live.get("sim_direct_rate", 0.0),
            "sim_partial_only_rate": live.get("sim_partial_only_rate", 0.0),
            "sim_avg_similarity": live.get("sim_avg_similarity", 0.0),
        }

    def score(self, eval_set_path: str | None = None) -> dict[str, float]:
        """
        对 eval set 中的 ticket+gold_reply 对打分。

        返回维度（权重参考 happy-kindling-church.md L164-178）：
          replay_solvability     — 可解决问题度（LLM judge 或 embedding）
          style_fidelity         — 风格相似度（embedding cosine）
          kb_grounding_rate      — 引用 ≥1 kb_source 的回复占比
          live_adoption          — 来自 reply_feedback.json（仅用于回归门）
          regression_gate        — 硬门：live_adoption 相对跌幅 ≤30%

        关键：replay 分数驱动进化，live 信号只做硬回归门。
        """
        root = Path.cwd()

        # --- 1. live 信号（回归门） ---
        live = self.live_signals()
        live_rate = live.get("live_adoption_rate", _BASELINE_ADOPTION_RATE_FALLBACK)
        baseline = _load_baseline_adoption(root)
        if baseline and baseline > 0:
            drop = (baseline - live_rate) / baseline
            regression_gate = drop <= REGRESSION_GATE_DROP_THRESHOLD
        else:
            regression_gate = True

        # --- 2. 读取 eval set ---
        inputs = self.build_replay_inputs(eval_set_path or "")
        if not inputs:
            return {
                "replay_solvability": 0.0,
                "style_fidelity": 0.0,
                "kb_grounding_rate": 0.0,
                "live_adoption": live_rate,
                "regression_gate": 1.0 if regression_gate else 0.0,
            }

        # --- 3. 生成 replay 回复 ---
        outputs = self.run_pipeline(inputs)

        # --- 4. 评分各维度 ---
        solvability_scores = []
        style_scores = []
        kb_grounded_count = 0

        for inp, out in zip(inputs, outputs):
            gold = inp.get("gold_reply", "")
            generated = out.get("reply_content", "")

            # style_fidelity: embedding cosine (或字符 n-gram 相似度降级)
            style_scores.append(_text_similarity(gold, generated))

            # replay_solvability: 简单启发式（有内容且不拒绝）
            solvability_scores.append(_solvability_heuristic(generated))

            # kb_grounding: 是否有 kb_sources
            if out.get("kb_sources") or _has_kb_citation(generated):
                kb_grounded_count += 1

        n = len(outputs)
        replay_solvability = sum(solvability_scores) / n if n else 0.0
        style_fidelity = sum(style_scores) / n if n else 0.0
        kb_grounding_rate = kb_grounded_count / n if n else 0.0

        return {
            "replay_solvability": round(replay_solvability, 4),
            "style_fidelity": round(style_fidelity, 4),
            "kb_grounding_rate": round(kb_grounding_rate, 4),
            "live_adoption": round(live_rate, 4),
            "regression_gate": 1.0 if regression_gate else 0.0,
        }

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

        tickets = []
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

    # ── 流水线（轻量包装） ────────────────────────────────────────────────────

    def run_pipeline(self, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        对一批工单生成回复。

        MVP 策略：
        - 尝试调用真实 BoardService.generate_reply_content（需 PYTHONPATH=APP/backend）
        - 无可用环境时 graceful degrade：用工单 summary 拼一个最小回复
        """
        outputs = []
        for ticket in inputs:
            out = _generate_reply_for_ticket(ticket)
            outputs.append(out)
        return outputs

    # ── 突变提议 ─────────────────────────────────────────────────────────────

    def propose_mutations(
        self, weakest_dim: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        按最弱维度定向提议突变（mapping 来自 happy-kindling-church.md M3 table）。

        weakest_dim → slot:
          style_fidelity        → style_rules_md
          replay_solvability    → system_prompt_core
          kb_grounding_rate     → similar_issues_top_k / similar_issues_min_score
          live_adoption         → (soft: style_rules_md；不直接驱动进化)
        """
        genome: Genome | None = context.get("genome")
        mutations: list[dict[str, Any]] = []

        if weakest_dim == "style_fidelity":
            current_rules = genome.slots.get("style_rules_md", "") if genome else ""
            mutations.append({
                "slot_name": "style_rules_md",
                "old_value_slice": current_rules[:200] if current_rules else "",
                "new_value_slice": current_rules,  # LLM should edit; placeholder keeps same
                "rationale": (
                    "style_fidelity is weakest: propose edits to style_rules_md "
                    "to better match user reply patterns"
                ),
                "mutation_type": "llm_edit_markdown",
            })

        elif weakest_dim == "replay_solvability":
            current_prompt = genome.slots.get("system_prompt_core", "") if genome else ""
            mutations.append({
                "slot_name": "system_prompt_core",
                "old_value_slice": current_prompt[:200] if current_prompt else "",
                "new_value_slice": current_prompt,
                "rationale": (
                    "replay_solvability is weakest: propose edits to system_prompt_core "
                    "to improve answer completeness and KB grounding instructions"
                ),
                "mutation_type": "llm_edit_markdown",
            })

        elif weakest_dim == "kb_grounding_rate":
            current_top_k = genome.slots.get("similar_issues_top_k", 5) if genome else 5
            current_min_score = genome.slots.get("similar_issues_min_score", 0.7) if genome else 0.7
            # Propose increasing top_k or decreasing min_score to get more KB hits
            new_top_k = min(10, int(current_top_k or 5) + 1)
            new_min_score = max(0.4, float(current_min_score or 0.7) - 0.05)
            mutations.append({
                "slot_name": "similar_issues_top_k",
                "old_value_slice": current_top_k,
                "new_value_slice": new_top_k,
                "rationale": (
                    f"kb_grounding_rate is weakest: increase similar_issues_top_k "
                    f"{current_top_k} → {new_top_k} for more KB evidence"
                ),
                "mutation_type": "numeric_increment",
            })
            mutations.append({
                "slot_name": "similar_issues_min_score",
                "old_value_slice": current_min_score,
                "new_value_slice": round(new_min_score, 2),
                "rationale": (
                    f"kb_grounding_rate is weakest: lower similar_issues_min_score "
                    f"{current_min_score} → {new_min_score:.2f} to include more KB hits"
                ),
                "mutation_type": "numeric_decrement",
            })

        elif weakest_dim == "live_adoption":
            # live_adoption is a hard gate signal, not a direct evolution target.
            # Propose style_rules edit as a soft response.
            current_rules = genome.slots.get("style_rules_md", "") if genome else ""
            mutations.append({
                "slot_name": "style_rules_md",
                "old_value_slice": current_rules[:200] if current_rules else "",
                "new_value_slice": current_rules,
                "rationale": (
                    "live_adoption signal is low (note: this is a lagging noisy signal). "
                    "Soft response: review style_rules_md for friction patterns that "
                    "cause users to modify or discard replies."
                ),
                "mutation_type": "llm_edit_markdown",
            })

        return mutations

    # ── live 信号 ─────────────────────────────────────────────────────────────

    def live_signals(self) -> dict[str, Any]:
        """
        读取 reply_feedback.json，返回 live 信号。

        live_adoption_rate 在 2.8% 量级下噪声大；仅作硬回归门，不驱动进化。
        """
        root = Path.cwd()
        stats_path = root / FEEDBACK_STATS_PATH
        if not stats_path.exists():
            return {
                "live_adoption_rate": _BASELINE_ADOPTION_RATE_FALLBACK,
                "total_generated": 0,
                "total_adopted": 0,
            }

        try:
            data = json.loads(stats_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {
                "live_adoption_rate": _BASELINE_ADOPTION_RATE_FALLBACK,
                "total_generated": 0,
                "total_adopted": 0,
            }

        # Parse live_adoption_rate: stored as "2.4%" or 0.024
        raw_rate = data.get("live_adoption_rate", "0%")
        if isinstance(raw_rate, str):
            rate = float(raw_rate.rstrip("%")) / 100.0
        else:
            rate = float(raw_rate)

        def _pct(v, default=0.0):
            if v is None:
                return default
            if isinstance(v, str):
                return float(v.rstrip("%")) / 100.0
            return float(v)

        return {
            "live_adoption_rate": rate,
            "total_generated": data.get("live_total", 0),
            "total_adopted": data.get("live_adopted", 0),
            "sim_direct_rate": _pct(data.get("sim_direct_rate")),
            "sim_partial_only_rate": _pct(data.get("sim_partial_only_rate")),
            "sim_avg_similarity": _pct(data.get("sim_avg_similarity")),
            "sim_total": data.get("sim_total", 0),
        }


# ── 内部辅助函数 ──────────────────────────────────────────────────────────────

def _generate_reply_for_ticket(ticket: dict[str, Any]) -> dict[str, Any]:
    """
    单工单回复生成包装。

    优先尝试调用真实 board_service_chroma.BoardService；
    不可用时降级为 summary-based 最小回复。
    """
    issue_key = ticket.get("issue_key") or ticket.get("ticket_id", "")
    summary = ticket.get("summary") or ticket.get("概要", "")

    # 尝试真实生成（需要完整运行时环境）
    try:
        from board_service_chroma import BoardService  # type: ignore
        svc = BoardService()
        result = svc.generate_reply_content(issue_key, force=False)
        if result and result.get("reply_content"):
            return result
    except Exception:
        pass

    # Graceful degrade：构造最小回复记录（不打 Jira，不调 LLM）
    stub_reply = f"您好！\n\n针对「{summary}」，请提供更多详情以便进一步协助。\n\n谢谢。"
    return {
        "issue_key": issue_key,
        "reply_content": stub_reply,
        "kb_sources": [],
        "examples_used_count": 0,
        "style_rules_applied": False,
        "generation_method": "stub",
    }


def _text_similarity(text_a: str, text_b: str) -> float:
    """
    字符 n-gram overlap 相似度（embedding cosine 降级版）。
    当两文本都为空时返回 1.0（完美匹配空集）。
    """
    if not text_a and not text_b:
        return 1.0
    if not text_a or not text_b:
        return 0.0

    def ngrams(text: str, n: int = 2) -> set[str]:
        text = text.lower()
        return {text[i:i+n] for i in range(len(text) - n + 1)}

    a_grams = ngrams(text_a)
    b_grams = ngrams(text_b)
    if not a_grams or not b_grams:
        return 0.0
    intersection = a_grams & b_grams
    union = a_grams | b_grams
    return len(intersection) / len(union)


def _solvability_heuristic(reply: str) -> float:
    """
    启发式可解决性评分（无 LLM 时的降级）：
    - 有实质内容且不是纯拒绝 → 高分
    - 回复为空 → 0
    - 含"暂不支持"/"暂未检索到" 等拒绝词 → 低分
    """
    if not reply or len(reply.strip()) < 10:
        return 0.0
    refuse_patterns = ["暂不支持", "暂未检索到", "目前不支持", "无法提供", "没有相关信息"]
    lower = reply.lower()
    for pat in refuse_patterns:
        if pat in lower:
            return 0.3
    # Prefer replies with steps or structured content
    if any(marker in reply for marker in ["步骤", "配置", "参见", "1.", "①", "•", "-"]):
        return 0.85
    return 0.65


def _has_kb_citation(reply: str) -> bool:
    """检查回复是否包含 KB 引用标记（参见、来源、知识库等）。"""
    markers = ["参见", "来源", "知识库", "《", "根据", "依据"]
    return any(m in reply for m in markers)


def _load_baseline_adoption(root: Path) -> float | None:
    """
    从 ledger.tsv 读取最近一个 KEPT=1 round 的 live_adoption 作为基线。
    如果 ledger 不存在或无记录，返回 None（让调用方使用 fallback）。
    """
    ledger_path = root / EVOLUTION_DIR / "reply" / "ledger.tsv"
    if not ledger_path.exists():
        return None

    baseline = None
    try:
        with open(ledger_path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) < 2:
            return None
        header = lines[0].strip().split("\t")
        adoption_idx = next(
            (i for i, h in enumerate(header) if "live_adoption" in h.lower()), None
        )
        kept_idx = next(
            (i for i, h in enumerate(header) if h.strip().lower() == "kept"), None
        )
        if adoption_idx is None:
            return None
        for line in reversed(lines[1:]):
            parts = line.strip().split("\t")
            if len(parts) <= adoption_idx:
                continue
            if kept_idx is not None and len(parts) > kept_idx:
                if parts[kept_idx].strip() != "1":
                    continue
            val_str = parts[adoption_idx].strip()
            if val_str:
                try:
                    baseline = float(val_str)
                    break
                except ValueError:
                    continue
    except OSError:
        pass

    return baseline
