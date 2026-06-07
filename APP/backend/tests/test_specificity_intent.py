"""TDD: G4 specificity 工单意图维度 + 统一 level（run_id=pdf-g4-intent）。

覆盖 docs/user-stories/pdf-g4-intent.json：
- A: _apply_intent_cap 纯函数规则表（inquiry 封顶 / GUIDE 逃逸 / how_to 不降 / 消歧 / 向后兼容）
- A: _compute_specificity_level 集成（title 参数向后兼容 + intent 封顶）
- B: gateway _run_g4 接受 specificity_level（传则用、不传 count 回退）
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

_BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))


# ── A. _apply_intent_cap 纯函数 ───────────────────────────────────────────────
def test_inquiry_caps_high_to_medium():
    from board_service_chroma import _apply_intent_cap
    assert _apply_intent_cap("high", "公有云能不能支持X客开") == "medium"
    assert _apply_intent_cap("high", "是否支持单点登录") == "medium"
    assert _apply_intent_cap("high", "有没有批量导入功能") == "medium"


def test_guide_escape_keeps_high():
    # LCZX-63363 实例：含 inquiry 词但明确要客开方案 → GUIDE 逃逸，不封顶
    from board_service_chroma import _apply_intent_cap
    assert _apply_intent_cap("high", "有没有做制单人加签的规划，能否指导提供下客开方案") == "high"
    assert _apply_intent_cap("high", "能不能实现自动审批，给个解决方案") == "high"


def test_howto_not_downgraded():
    from board_service_chroma import _apply_intent_cap
    assert _apply_intent_cap("high", "如何配置审批流条件分支") == "high"
    assert _apply_intent_cap("high", "在哪里设置连岗方式") == "high"


def test_howto_inquiry_disambig_howto_wins():
    from board_service_chroma import _apply_intent_cap
    assert _apply_intent_cap("high", "如何判断是否支持X") == "high"


def test_backward_compat_and_neutral():
    from board_service_chroma import _apply_intent_cap
    assert _apply_intent_cap("high", "") == "high"            # 空 title
    assert _apply_intent_cap("medium", "能不能X") == "medium"  # 非 high 不封顶
    assert _apply_intent_cap("low", "能不能X") == "low"
    assert _apply_intent_cap("none", "能不能X") == "none"
    assert _apply_intent_cap("high", "审批流报错500无法保存") == "high"  # neutral 无 inquiry 词


# ── A. _compute_specificity_level 集成（__new__ 绕 init，不锁 chroma）─────────────
def _bs():
    import board_service_chroma
    return board_service_chroma.BoardService.__new__(board_service_chroma.BoardService)


_KB_HIGH = [{"step_density": 1.0, "source_tier": 3, "completeness": 1.0, "score": 1.0}]


def test_compute_specificity_backward_compat():
    bs = _bs()
    assert bs._compute_specificity_level(_KB_HIGH) == "high"            # 不传 title == 现状
    assert bs._compute_specificity_level(_KB_HIGH, title="") == "high"


def test_compute_specificity_inquiry_cap():
    bs = _bs()
    assert bs._compute_specificity_level(_KB_HIGH, title="公有云能不能支持X") == "medium"
    assert bs._compute_specificity_level(_KB_HIGH, title="如何配置X") == "high"
    assert bs._compute_specificity_level(_KB_HIGH, title="能否指导提供客开方案") == "high"  # GUIDE 逃逸


# ── B. gateway _run_g4 specificity_level 透传 ────────────────────────────────
def _gw():
    from services.reply_gateway import ReplyGateway
    return ReplyGateway(vector_store=MagicMock(), reply_trainer=MagicMock())


def test_run_g4_uses_external_level():
    gw = _gw()
    kb2 = [{"name": "a", "score": 0.9}, {"name": "b", "score": 0.8}]
    # 传 specificity_level → 用它(单一真相源，覆盖 count==2 本应 medium)
    assert gw._run_g4(kb2, specificity_level="high")["level"] == "high"
    assert gw._run_g4(kb2, specificity_level="low")["verdict"] == "warn"
    assert gw._run_g4(kb2, specificity_level="none")["verdict"] == "fail"


def test_run_g4_count_fallback():
    gw = _gw()
    kb2 = [{"name": "a", "score": 0.9}, {"name": "b", "score": 0.8}]
    # 不传 → count 回退(向后兼容，现有 UT-G4 不破)
    assert gw._run_g4(kb2)["level"] == "medium"          # count==2
    assert gw._run_g4([{"name": "a"}])["level"] == "low"  # count==1
    assert gw._run_g4([])["level"] == "none"              # count==0
