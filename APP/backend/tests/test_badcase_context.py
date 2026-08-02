"""badcase 现场快照上报的结构守卫。

覆盖两类【只靠 JS 断言查不出、必须靠结构检查锁住】的回归：
1. 快捷笔记弹窗的 z-index 必须高于回复编辑器，否则从智能回复窗口点「反馈问题」
   打开的弹窗会被压在底下、完全无法操作（实测踩过）。
2. 后端超大快照裁剪必须【先扔正文、保住五道闸】，而不是整条丢弃。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BOARD_HTML = Path(__file__).resolve().parents[2] / "frontend" / "board.html"


def _z(html: str, element_id: str) -> int:
    """取某个元素 class 里的 z-[N]。"""
    m = re.search(r'id="%s"[^>]*class="([^"]*)"' % re.escape(element_id), html)
    assert m, f"未找到 #{element_id}"
    z = re.search(r"z-\[(\d+)\]", m.group(1))
    assert z, f"#{element_id} 没有 z-[N]: {m.group(1)}"
    return int(z.group(1))


def test_badcase_modal_stacks_above_reply_editor():
    html = BOARD_HTML.read_text(encoding="utf-8")
    note = _z(html, "kb-quick-note-modal")
    editor = _z(html, "replyEditorModal")
    assert note > editor, (
        f"快捷笔记/Badcase 弹窗 z={note} 必须高于回复编辑器 z={editor}，"
        "否则从智能回复窗口一键上报时弹窗被遮挡"
    )


def test_reply_editor_has_badcase_entry():
    html = BOARD_HTML.read_text(encoding="utf-8")
    assert "openReplyBadcase()" in html, "回复编辑器缺少一键反馈入口"
    assert "_collectReplyContext" in html, "缺少现场快照采集函数"
    # 快照必须在 updateReplyBasis 之后留存，否则拿不到回填的五道闸
    body = html[html.index("async function updateGenDetails("):]
    body = body[: body.index("\n        }")]
    assert body.index("await updateReplyBasis(data)") < body.index("__lastReplyMeta"), (
        "__lastReplyMeta 必须在 updateReplyBasis 之后赋值，否则 reply_gateway 尚未回填"
    )


def test_context_shrink_keeps_gates_drops_body():
    from services.badcase_context import (
        shrink_badcase_context as _shrink_badcase_context,
        BADCASE_CONTEXT_MAX_BYTES as _BADCASE_CONTEXT_MAX_BYTES,
    )

    ctx = {
        "trace_id": "rp-test",
        "issue_key": "ABC-1",
        "captured_at": "2026-08-02T00:00:00Z",
        "reply_text": "填充" * 40000,
        "ai_analysis": {"problem_analysis": "分析" * 5000},
        "ui": {"reply_method": "x"},
        "gates": {f"G{i}": {"verdict": "pass"} for i in range(1, 6)},
        "final_action": "reply",
    }
    out = _shrink_badcase_context(ctx)
    assert out["_truncated"] is True
    assert out["gates"], "裁剪必须保住五道闸"
    assert out["trace_id"] == "rp-test", "裁剪必须保住 trace_id"
    assert "reply_text" not in out, "裁剪应优先丢弃大块正文"
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= _BADCASE_CONTEXT_MAX_BYTES


def test_context_shrink_passthrough_small():
    from services.badcase_context import shrink_badcase_context as _shrink_badcase_context

    assert _shrink_badcase_context(None) is None
    small = {"trace_id": "t", "gates": {"G1": {"verdict": "pass"}}}
    assert _shrink_badcase_context(small) == small, "小快照应原样透传"
