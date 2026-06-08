"""Gate 5 独立监督审计：用不同 LLM provider 评审生成回复的质量。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_GATES_YAML = _PROJECT_ROOT / "config" / "reply_gates.yaml"

_PROVIDER_ISOLATION: dict = {
    "minimax": ["local", "minimax"],   # zhipu=GLM-5.1(reasoning-only) → 降级用 minimax
    "zhipu":   ["local", "minimax"],
    "local":   ["minimax", "zhipu"],
    "gemini":  ["minimax", "zhipu"],
}


def _get_llm_for_gate(node_key: str, main_provider: str) -> str:
    """返回监督 LLM provider：优先读 llm_feature_routing.json reply_supervisor key，避免 local 超时。

    兼容降级链 list 配置（如 ["zhipu","local"]）：取首个非 local 的 provider。
    历史教训：本函数曾把 list 原样当 provider 名返回 → "No API Key provided" → G5 全 llm_failed。
    """
    try:
        routing_file = _PROJECT_ROOT / "llm_feature_routing.json"
        routing = json.loads(routing_file.read_text(encoding="utf-8"))
        feature_val = routing.get("reply_supervisor") or routing.get("_default", "zhipu")
        if isinstance(feature_val, list):
            feature_provider = next((p for p in feature_val if p and p != "local"),
                                    feature_val[0] if feature_val else "")
        else:
            feature_provider = feature_val
        if feature_provider and feature_provider != "local":
            logger.debug("[_get_llm_for_gate] using feature routing: reply_supervisor → %s", feature_provider)
            return feature_provider
    except Exception as exc:
        logger.debug("[_get_llm_for_gate] feature routing lookup failed: %s", exc)
    return _pick_supervisor_provider(main_provider)


@dataclass
class SupervisorResult:
    supervisor_score: Optional[float] = None
    risk_flags: list[str] = field(default_factory=list)
    evidence_coverage: float = 0.5
    step_safety: str = "safe"   # "safe" | "risky" | "unsafe"
    rationale: str = ""
    provider_used: str = ""
    gate_enabled: bool = True
    status: str = "ok"


def supervise(
    issue_key: str,
    issue_title: str,
    issue_description: str,
    generated_reply: str,
    kb_evidence: list[dict],
    gate_decisions: dict,
    main_provider: str = "minimax",
    deploy_mode: str = "",
) -> SupervisorResult:
    """
    调用独立 LLM 对生成回复进行质量审计。
    主 provider 与监督 provider 强制隔离，避免自评偏差。
    """
    cfg = _load_gate_config()
    if not cfg.get("enabled", False):
        return SupervisorResult(gate_enabled=False)

    provider = _get_llm_for_gate("supervisor", main_provider)

    # 构造证据摘要（最多 4 条，每条 800 字符）—— 与 reply 生成端 kb_evidence[:4]×1500 对齐（R3）。
    # 原 [:3]×300 让 supervisor 只看到约 1/5 的证据，结构性无法核实 reply 引用的证据出处/幻觉，
    # 扩窗后 G5 能基于与 reply LLM 同量级的证据判断 hallucination/evidence_mismatch。
    evidence_parts = []
    for i, item in enumerate((kb_evidence or [])[:4], 1):
        text = (item.get("chunk_text") or item.get("raw_content") or "")[:800]
        name = item.get("name", f"资料{i}")
        evidence_parts.append(f"[资料{i}] {name}: {text}")
    evidence_summary = "\n".join(evidence_parts) or "（无知识库证据）"

    _deploy_hint = ""
    if deploy_mode:
        _deploy_hint = f"部署模式：{deploy_mode}"
        if "公有云" in str(deploy_mode) or "yonsuite" in str(deploy_mode).lower():
            _deploy_hint += "（⚠️ 公有云客开受限：回复不得建议联系客开/客户化开发/二次开发，违者标 cloud_custom_dev）"
        _deploy_hint += "\n"

    prompt = (
        "你是一个独立的回复质量审计员，请评估以下客服回复的质量。\n\n"
        f"工单标题：{issue_title[:200]}\n"
        f"工单描述：{issue_description[:400]}\n"
        f"{_deploy_hint}\n"
        f"知识库证据：\n{evidence_summary}\n\n"
        f"待审回复：\n{generated_reply[:800]}\n\n"
        "请从以下维度评分，返回纯 JSON（不加代码块标记）：\n"
        "1. supervisor_score：综合质量分 0.0-1.0\n"
        "2. risk_flags：问题标签数组，可选值：hallucination（幻觉）/ evidence_mismatch（与证据不符）/ "
        "over_specific（过度具体化）/ user_intent_drift（偏离用户意图）/ version_conflict（版本冲突）/ "
        "cloud_custom_dev（公有云工单却建议走客开）\n"
        "3. evidence_coverage：知识库证据覆盖率 0.0-1.0\n"
        "4. step_safety：步骤安全性 safe / risky / unsafe\n"
        "5. rationale：简短审计说明（50字以内）\n\n"
        "格式：{\"supervisor_score\":0.8,\"risk_flags\":[],\"evidence_coverage\":0.75,"
        "\"step_safety\":\"safe\",\"rationale\":\"...\"}"
    )

    from services.local_llm_lifecycle import is_alive, shutdown_if_started_by_us
    started_local = False
    raw = ""
    try:
        if provider == "local":
            if is_alive():
                # 本地模型已在线，直接用；不由我们启动，结束后也不关
                started_local = False  # 已由别处管理，不触发 shutdown
            else:
                # 热路径不等待本地模型启动，立即降级到第二候选 provider
                candidates = _PROVIDER_ISOLATION.get(main_provider, ["minimax", "zhipu"])
                provider = next((c for c in candidates if c != "local"), "minimax")
                logger.info("[supervisor] local LLM offline, falling back to %s", provider)
        from llm_service import LLMService
        llm = LLMService()
        _pcfg = _load_provider_cfg(provider)
        # reasoning models (MiniMax-M2.7, DeepSeek-R1) need ≥4096 tokens to clear their think block
        max_tok = 4096 if provider in ("minimax", "local") else 1500
        raw = llm.call_llm(prompt, api_key=_pcfg["api_key"], provider=provider, model_name=_pcfg["model_name"], base_url=_pcfg["base_url"], max_tokens=max_tok, temperature=0.1)
        logger.info("[supervisor] %s scored by %s: %.80s", issue_key, provider, raw)
    except Exception as e:
        logger.warning("[supervisor] LLM call failed (%s), using neutral score: %s", provider, e)
        return SupervisorResult(supervisor_score=None, status="llm_failed", rationale="审计调用失败", provider_used=provider)
    finally:
        if started_local:
            shutdown_if_started_by_us("reply_supervisor")

    result = _parse_result(raw, provider)
    # 确定性后处理：公有云工单含客开建议 → 强制 cloud_custom_dev flag + 压分至 0.4，
    # 不依赖 LLM 自觉。压分使 direct 路径 _sup_g3_failed 触发降级、normal 路径阻断自动回复。
    try:
        from services.reply_reuse_evaluator import is_public_cloud, reply_suggests_custom_dev
        if is_public_cloud(deploy_mode) and reply_suggests_custom_dev(generated_reply):
            if "cloud_custom_dev" not in result.risk_flags:
                result.risk_flags.append("cloud_custom_dev")
            if result.supervisor_score is not None and result.supervisor_score > 0.4:
                result.supervisor_score = 0.4
            if result.step_safety == "safe":
                result.step_safety = "risky"
            logger.info("[supervisor] %s 公有云客开违规 → 强制 cloud_custom_dev flag, score→0.4", issue_key)
    except Exception as _e_cloud:
        logger.debug("[supervisor] cloud policy post-check failed: %s", _e_cloud)
    return result


def _load_provider_cfg(provider_name: str) -> dict:
    try:
        raw = json.loads((_PROJECT_ROOT / "llm_config.json").read_text(encoding="utf-8"))
        p = raw.get(provider_name, {})
        return {"api_key": p.get("api_key", ""), "model_name": p.get("model_name", ""), "base_url": p.get("base_url", "")}
    except Exception:
        return {"api_key": "", "model_name": "", "base_url": ""}


def _pick_supervisor_provider(main_provider: str) -> str:
    candidates = _PROVIDER_ISOLATION.get(main_provider, ["zhipu", "minimax"])
    # 优先尝试 local（节省成本），但不做 ensure_running（在 supervise 内处理）
    return candidates[0] if candidates else "zhipu"


def _parse_result(raw: str, provider: str) -> SupervisorResult:
    # 提取 JSON（兼容 LLM 可能输出的前缀/后缀文本）
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        logger.warning("[supervisor] no JSON found in response, using neutral score")
        return SupervisorResult(supervisor_score=None, status="llm_failed", rationale="无法解析审计结果", provider_used=provider)
    try:
        d = json.loads(m.group(0))
        return SupervisorResult(
            supervisor_score=float(d.get("supervisor_score", 0.5)),
            risk_flags=list(d.get("risk_flags", [])),
            evidence_coverage=float(d.get("evidence_coverage", 0.5)),
            step_safety=str(d.get("step_safety", "safe")),
            rationale=str(d.get("rationale", ""))[:200],
            provider_used=provider,
            status="ok",
        )
    except Exception as e:
        logger.warning("[supervisor] JSON parse error: %s", e)
        return SupervisorResult(supervisor_score=None, status="llm_failed", rationale="JSON解析异常", provider_used=provider)


def _load_gate_config() -> dict:
    try:
        raw = yaml.safe_load((_PROJECT_ROOT / "config" / "reply_gates.yaml").read_text(encoding="utf-8"))
        return raw.get("gates", {}).get("supervisor", {})
    except Exception:
        return {}
