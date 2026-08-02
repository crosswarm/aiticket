"""badcase 现场快照的体积裁剪。

单独成模块而非塞在 main.py 里，是为了让它能被单测直接 import ——
main.py 一 import 就会拉起整个应用（auth_service / chroma / LLM 客户端…），
在只装了标准库的环境里根本跑不起来，纯函数没理由陪绑。

只依赖标准库。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

# badcases.jsonl 是逐行追加的诊断日志，单条过大会拖垮后续读取与分析。
BADCASE_CONTEXT_MAX_BYTES = 64 * 1024

# 超限时的丢弃顺序：先扔大块自由文本，保住 trace_id 与五道闸这类结构化定位信息。
_DROP_ORDER = ("reply_text", "ai_analysis", "ui")


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def shrink_badcase_context(
    ctx: Optional[Dict[str, Any]],
    max_bytes: int = BADCASE_CONTEXT_MAX_BYTES,
) -> Optional[Dict[str, Any]]:
    """把上报快照压到体积上限内。

    - 未超限：原样返回（不复制、不加标记）。
    - 超限：按 _DROP_ORDER 逐个丢弃并打 _truncated 标记，每丢一个就重新称重。
    - 仍超限：退化为只留 trace_id 等最小定位信息，保证还能和服务端日志对上号。
    """
    if not ctx:
        return None
    try:
        if _size(ctx) <= max_bytes:
            return ctx
        shrunk = dict(ctx)
        for field in _DROP_ORDER:
            shrunk.pop(field, None)
            shrunk["_truncated"] = True
            if _size(shrunk) <= max_bytes:
                return shrunk
        return {
            "trace_id": ctx.get("trace_id", ""),
            "issue_key": ctx.get("issue_key", ""),
            "captured_at": ctx.get("captured_at", ""),
            "_truncated": True,
            "_note": "上下文过大已裁剪，仅保留 trace_id 供服务端日志比对",
        }
    except Exception as exc:  # 快照来自前端，不可信；任何异常都不该阻断上报主流程
        return {"_note": f"上下文序列化失败: {exc}", "_truncated": True}
