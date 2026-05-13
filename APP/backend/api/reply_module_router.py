"""模块感知智能回复 API — POST /api/reply/generate-by-module + GET /api/reply/module-coverage"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/reply", tags=["reply-module"])


class GenerateByModuleRequest(BaseModel):
    issue_key: str
    module: Optional[str] = None       # 强制指定模块；None 时自动推断
    force: bool = False                 # True=跳过回复缓存


@router.post("/generate-by-module")
def generate_by_module(req: GenerateByModuleRequest, raw_request: Request):
    """按模块生成回复。module 指定时覆盖自动推断；空时走自动推断（同 /api/board/generate-reply）。"""
    try:
        from board_service_chroma import BoardService
        # 获取主服务实例（main.py 已实例化，通过 app.state 或直接 import 全局）
        try:
            import main as _main
            _board_service = _main.board_service
        except Exception:
            raise HTTPException(status_code=503, detail="board_service 未初始化")

        result = _board_service.generate_reply_content(req.issue_key, force=req.force)
        if result.get("error"):
            raise HTTPException(status_code=422, detail=result["error"])

        # 推断模块（用于返回给调用方参考）
        ai_analysis = result.get("ai_analysis") or {}
        inferred_module = req.module or BoardService._resolve_module_category(ai_analysis)

        return {
            "status": "success",
            "reply": result.get("solution_content", result.get("reply_content", "")),
            "kb_refs": [
                {
                    "name": item.get("name", ""),
                    "module": item.get("l1_module", ""),
                    "score": round(item.get("score", 0), 3),
                }
                for item in (result.get("kb_evidence") or [])[:4]
            ],
            "module_used": inferred_module,
            "module_match_score": None,   # 留给未来打分扩展
            "fallback_used": inferred_module is None,
            "cached": result.get("cached", False),
            "word_count": result.get("word_count", 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/module-coverage")
def module_coverage(module: str, raw_request: Request):
    """查询某模块在 KB / 样例 中的覆盖度，供调用方判断该模块能否使用智能回复。"""
    try:
        import sqlite3
        from pathlib import Path

        base = Path(__file__).resolve().parent.parent
        db_path = base.parent.parent / "data" / "sqlite" / "kb_chunks.db"

        # KB 文档覆盖
        kb_total = kb_module = 0
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                (kb_total,) = conn.execute("SELECT COUNT(DISTINCT content_id) FROM chunks").fetchone()
                (kb_module,) = conn.execute(
                    "SELECT COUNT(DISTINCT content_id) FROM chunks WHERE l1_module = ?", (module,)
                ).fetchone()
            finally:
                conn.close()

        # 样例覆盖（reply_trainer stats by_module）
        sample_count = adopted_count = 0
        try:
            from reply_trainer import ReplyTrainer
            import main as _main
            _stats = _main.board_service.reply_trainer._stats if hasattr(_main, "board_service") else {}
            bm = _stats.get("by_module", {}).get(module, {})
            sample_count = bm.get("total", 0)
            adopted_count = bm.get("adopted", 0)
        except Exception:
            pass

        coverage_level = "high" if kb_module >= 50 else ("medium" if kb_module >= 10 else "low")

        return {
            "module": module,
            "kb_docs_total": kb_total,
            "kb_docs_module": kb_module,
            "kb_coverage_pct": round(kb_module / kb_total * 100, 1) if kb_total else 0,
            "coverage_level": coverage_level,
            "reply_examples_total": sample_count,
            "reply_examples_adopted": adopted_count,
            "recommendation": (
                "可直接使用模块感知智能回复" if coverage_level == "high"
                else "覆盖有限，建议先补充该模块的 KB 文档后使用" if coverage_level == "medium"
                else "覆盖不足，建议先通过知识库管理补充该模块文档"
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
