"""G3 复用 direct 档必须保持禁用。

背景：direct 复用在 board_service_chroma 的 G3 分支**短路 return**，
绕过后续所有 guard（含 G5 supervisor），实测出现过复用错工单回复的事故。
2026-06-11 起用 `min_similarity_direct: 1.01`（恒不可达）禁用该档。

但这一行此前只落在内部 aiticket/main，生产 offline-deploy 的
`gates.reuse` 段一直缺这个键 → 吃 reply_reuse_evaluator._load_gate_config 的
默认值 0.82 → direct 档在生产上实际处于**启用**状态。本测试钉死配置不再漂移。

要恢复 direct 档时，请连同 G5 fail-closed 的现状一起复核，再改本测试。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

_GATES_YAML = pathlib.Path(__file__).resolve().parent.parent / "config" / "reply_gates.yaml"

# 与 services/reply_reuse_evaluator.py:_load_gate_config 保持同一读取路径
_CONFIG_PATH = ("gates", "reuse")

# 与 services/reply_reuse_evaluator.py 的函数签名默认值保持一致
_EVALUATOR_DEFAULT_MIN_SIM_DIRECT = 0.82


@pytest.fixture(scope="module")
def reuse_cfg() -> dict:
    raw = yaml.safe_load(_GATES_YAML.read_text(encoding="utf-8"))
    cfg = raw
    for key in _CONFIG_PATH:
        assert isinstance(cfg, dict) and key in cfg, (
            f"reply_gates.yaml 缺少 {'.'.join(_CONFIG_PATH)} 段——"
            f"reply_reuse_evaluator._load_gate_config 会拿到 {{}} 并全部吃默认值"
        )
        cfg = cfg[key]
    return cfg


def test_min_similarity_direct_is_explicitly_set(reuse_cfg: dict) -> None:
    """必须显式配置——缺省会静默回落到 0.82，direct 档就活了。"""
    assert "min_similarity_direct" in reuse_cfg, (
        "gates.reuse.min_similarity_direct 未显式配置。缺省时 "
        f"reply_reuse_evaluator 会用默认值 {_EVALUATOR_DEFAULT_MIN_SIM_DIRECT}，"
        "direct 档将被启用并绕过 G5 supervisor。"
    )


def test_direct_tier_is_unreachable(reuse_cfg: dict) -> None:
    """阈值必须 > 1.0——余弦相似度不可能超过 1，即 direct 档恒不可达。"""
    value = float(reuse_cfg["min_similarity_direct"])
    assert value > 1.0, (
        f"gates.reuse.min_similarity_direct = {value}，direct 档处于启用状态。"
        "该档会短路 return 绕过 G5 supervisor，历史上导致复用错工单回复。"
        "恢复前需先确认 G5 已 fail-closed 且阈值经真实采纳样本重标定。"
    )


def test_tier_decision_rejects_direct_at_perfect_similarity(reuse_cfg: dict) -> None:
    """行为级验证：即便 sim=1.0、composite 拉满、adopted=True，也不得判 direct。

    只断配置值不够——真正要防的是 `_assign_tier` 返回 "direct"。
    """
    from services.reply_reuse_evaluator import _assign_tier

    tier = _assign_tier(
        composite=1.0,
        example={"adopted": True},
        composite_threshold=float(reuse_cfg.get("composite_threshold", 0.83)),
        llm_blend_min=float(reuse_cfg.get("llm_blend_min", 0.60)),
        sim=1.0,  # 完美相似度——余弦的理论上界
        min_sim_blend=float(reuse_cfg.get("min_similarity_blend", 0.70)),
        min_sim_direct=float(reuse_cfg["min_similarity_direct"]),
    )
    assert tier != "direct", (
        f"sim=1.0 时仍判为 direct（得到 {tier!r}），说明 min_similarity_direct "
        "没能封住该档。direct 会短路 return 绕过所有后续 guard。"
    )
    assert tier == "llm_blend", f"预期降级为 llm_blend，实际 {tier!r}"
