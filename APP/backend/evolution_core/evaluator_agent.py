"""
EvaluatorAgent：独立 LLM 评审员
- 优先使用 zhipu/GLM（避免与 classify/reply 共用 minimax 产生同品牌偏见）
- v5 教训：MAX_TOKENS >= 4096 for reasoning models
- v5 教训：Path.cwd() for config loading，never Path(__file__).parent
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from APP.backend.evolution_core.constants import (
    MAX_TOKENS_REASONING,
    MAX_TOKENS_STANDARD,
    BATCH_SIZE_REASONING,
)


def _load_llm_config() -> dict[str, Any]:
    """
    Load LLM config from APP/backend/llm_config.json.
    Uses Path.cwd() — must be called from project root.
    """
    config_path = Path.cwd() / "APP/backend/llm_config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"llm_config.json not found at {config_path}")
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _load_feature_routing() -> dict[str, str]:
    routing_path = Path.cwd() / "APP/backend/llm_feature_routing.json"
    if routing_path.exists():
        try:
            with open(routing_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _get_evaluator_client():
    """
    Return (openai.OpenAI client, model_name, is_reasoning).
    Priority: feature routing → zhipu → aliyun → openai.
    """
    try:
        import openai
    except ImportError as e:
        raise ImportError("openai package required: pip install openai") from e

    cfg = _load_llm_config()
    routing = _load_feature_routing()
    routed_provider = routing.get("darwin_eval") or routing.get("_default")

    provider_order = ["zhipu", "aliyun", "openai"]
    if routed_provider and routed_provider in cfg:
        provider_order = [routed_provider] + [p for p in provider_order if p != routed_provider]

    for provider in provider_order:
        if provider == "local":
            try:
                from APP.backend.services.local_llm_lifecycle import ensure_running as _ensure_local
                if not _ensure_local():
                    continue  # 三次自启失败，降级到下一个 provider
            except Exception:
                pass
        if provider in cfg:
            p = cfg[provider]
            if not p.get("api_key"):
                continue
            client = openai.OpenAI(
                api_key=p["api_key"],
                base_url=p.get("base_url"),
            )
            model = p["model_name"]
            is_reasoning = "M2" in model or "think" in model.lower()
            return client, model, is_reasoning

    raise ValueError("No suitable LLM provider found in llm_config.json")


def evaluate(
    inputs: list[dict],
    outputs: list[dict],
    eval_criteria: dict,
) -> dict[str, float]:
    """
    Evaluate outputs against inputs using an LLM judge.

    Args:
        inputs: List of input dicts (e.g. ticket dicts with ticket_id, summary)
        outputs: List of output dicts (e.g. {ticket_id, classified_leaf, ...})
        eval_criteria: Dict of dimension -> description, e.g.
            {"leaf_purity_rate": "fraction of tickets correctly classified"}

    Returns:
        Dict of dimension -> score (0.0–1.0)
    """
    client, model, is_reasoning = _get_evaluator_client()
    max_tokens = MAX_TOKENS_REASONING if is_reasoning else MAX_TOKENS_STANDARD

    # Build batches
    batch_size = BATCH_SIZE_REASONING if is_reasoning else 20
    all_scores: dict[str, list[float]] = {dim: [] for dim in eval_criteria}

    pairs = list(zip(inputs, outputs))

    for i in range(0, len(pairs), batch_size):
        batch = pairs[i : i + batch_size]
        batch_inputs = [p[0] for p in batch]
        batch_outputs = [p[1] for p in batch]

        prompt = _build_eval_prompt(batch_inputs, batch_outputs, eval_criteria)

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个严格的质量评审员，按要求对分类结果打分，返回 JSON。",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            content = resp.choices[0].message.content or ""
            batch_scores = _parse_scores(content, eval_criteria)
            for dim, score in batch_scores.items():
                all_scores[dim].append(score)
        except Exception as e:
            # Log but continue; partial scores still useful
            import sys
            print(f"[evaluator_agent] batch {i} error: {e}", file=sys.stderr)

    # Average across batches
    final: dict[str, float] = {}
    for dim, scores in all_scores.items():
        final[dim] = sum(scores) / len(scores) if scores else 0.0

    return final


def _build_eval_prompt(
    inputs: list[dict],
    outputs: list[dict],
    eval_criteria: dict,
) -> str:
    criteria_lines = "\n".join(
        f"  - {dim}: {desc}" for dim, desc in eval_criteria.items()
    )
    pairs_json = json.dumps(
        [{"input": inp, "output": out} for inp, out in zip(inputs, outputs)],
        ensure_ascii=False,
        indent=2,
    )
    dims_list = list(eval_criteria.keys())
    return f"""请对以下 {len(inputs)} 条分类结果打分。

## 评分维度
{criteria_lines}

## 数据
{pairs_json}

## 输出格式
请返回纯 JSON，格式如下（分数 0.0~1.0）：
{json.dumps({dim: 0.0 for dim in dims_list}, ensure_ascii=False)}

只返回 JSON，不要其他内容。"""


def _parse_scores(content: str, eval_criteria: dict) -> dict[str, float]:
    """
    Parse LLM response into dimension scores.
    v5 lesson: always type-check parsed results.
    """
    import re

    # Strip markdown fences
    content = re.sub(r"```[^\n]*\n?", "", content).strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON object
        m = re.search(r"\{[^{}]+\}", content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return {dim: 0.0 for dim in eval_criteria}
        else:
            return {dim: 0.0 for dim in eval_criteria}

    if not isinstance(data, dict):
        return {dim: 0.0 for dim in eval_criteria}

    result: dict[str, float] = {}
    for dim in eval_criteria:
        val = data.get(dim, 0.0)
        try:
            result[dim] = float(val)
        except (TypeError, ValueError):
            result[dim] = 0.0

    return result
