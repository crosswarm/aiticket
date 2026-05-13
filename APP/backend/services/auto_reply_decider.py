"""多维加权 Auto-Reply 决策：supervisor_score × product_priority × customer_importance_inverse。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).parent.parent

_PRODUCT_PRIORITY = {"yonsuite": 1.0, "standard": 0.7, "custom": 0.4}
_DEFAULT_THRESHOLDS = {
    "yonsuite": {"normal": 0.65, "key_customer": 0.80},
    "standard": {"normal": 0.72, "key_customer": 0.85},
    "custom":   {"normal": 0.80, "key_customer": None},  # None = 不允许
}
_DEFAULT_WEIGHTS = {
    "supervisor_confidence": 0.50,
    "product_priority": 0.20,
    "customer_importance_inverse": 0.30,
}


@dataclass
class AutoReplyDecision:
    auto_reply: bool
    composite_score: float | None
    threshold: float | None   # None = 不允许 auto-reply
    action: str  # "auto_reply" | "auto_reply_low_risk" | "pending_batch_approve" | "manual_with_steps" | "manual_review" | "human_required"
    product_type: str
    is_key_customer: bool


def decide(
    supervisor_score: Optional[float],
    product_type: str = "standard",
    is_key_customer: bool = False,
    *,
    reuse_matched: bool = False,
    risk_flags: list = None,
) -> AutoReplyDecision:
    """
    综合 supervisor_score、产品优先级、客户重要度，决定是否自动回复。
    """
    if supervisor_score is None:
        return AutoReplyDecision(
            auto_reply=False,
            composite_score=None,
            threshold=None,
            action="needs_decision",
            product_type=product_type,
            is_key_customer=is_key_customer,
        )
    cfg = _load_config()
    if not cfg.get("enabled", False):
        return AutoReplyDecision(
            auto_reply=False,
            composite_score=supervisor_score,
            threshold=None,
            action="manual_review",
            product_type=product_type,
            is_key_customer=is_key_customer,
        )

    weights = {**_DEFAULT_WEIGHTS, **cfg.get("weights", {})}
    thresholds_cfg = cfg.get("thresholds", _DEFAULT_THRESHOLDS)

    product_priority_factor = _PRODUCT_PRIORITY.get(product_type, 0.7)
    customer_importance_factor = 1.0 if is_key_customer else 0.0

    composite = (
        supervisor_score * weights["supervisor_confidence"]
        + product_priority_factor * weights["product_priority"]
        + (1.0 - customer_importance_factor) * weights["customer_importance_inverse"]
    )
    composite = round(min(composite, 1.0), 4)

    # 取该 product_type 的阈值
    pt_thresholds = thresholds_cfg.get(product_type, _DEFAULT_THRESHOLDS.get(product_type, {}))
    if not pt_thresholds:
        pt_thresholds = _DEFAULT_THRESHOLDS.get("standard", {})

    threshold_key = "key_customer" if is_key_customer else "normal"
    threshold = pt_thresholds.get(threshold_key)

    if threshold is None:
        # 明确禁止 auto-reply（如重点客户×客开）
        return AutoReplyDecision(
            auto_reply=False,
            composite_score=composite,
            threshold=None,
            action="human_required",
            product_type=product_type,
            is_key_customer=is_key_customer,
        )

    auto_reply = composite >= threshold
    action = ""

    # VIP 专有路径：极高 supervisor + 无风险 + 历史复用 可绕过 composite 阈值直接自动回复
    if is_key_customer and not auto_reply:
        lr = cfg.get("low_risk_auto_threshold", {})
        flags = risk_flags or []
        if (supervisor_score >= lr.get("supervisor_score", 0.95)
                and (not lr.get("require_no_risk_flags", True) or len(flags) == 0)
                and (not lr.get("require_reuse_match", True) or reuse_matched)):
            auto_reply = True
            action = "auto_reply_low_risk"

    if not action:
        if not auto_reply:
            action = "manual_with_steps" if composite >= threshold * 0.85 else "manual_review"
        elif not is_key_customer and cfg.get("require_human_approval_for_normal_customer", True):
            # 非重点客户高置信度 → staging 待批准，不直接发送
            action = "pending_batch_approve"
            auto_reply = False
        else:
            action = "auto_reply"

    return AutoReplyDecision(
        auto_reply=auto_reply,
        composite_score=composite,
        threshold=float(threshold),
        action=action,
        product_type=product_type,
        is_key_customer=is_key_customer,
    )


def _load_config() -> dict:
    try:
        raw = yaml.safe_load((_PROJECT_ROOT / "config" / "reply_gates.yaml").read_text(encoding="utf-8"))
        return raw.get("auto_reply_decision", {})
    except Exception:
        return {}
