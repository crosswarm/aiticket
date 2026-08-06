"""LLM 调用埋点：每次调用落一条记录，失败时完整留存上游响应体。

为什么需要它
------------
系统此前对 LLM 调用**零计数**，回答"每天调了多少次""配额还剩多少"
只能靠 grep 日志反推，而这已经导致过两次错误结论：

1. 把日志行数当调用数——`[GenerateReply]` 的行绝大多数是 KB 搜索/缓存命中，
   据此算出 3.9 次/工单，实际是 1.9 次。
2. 把一次性观测当常量——从某次 429 报错里看到 `203/199`，
   就把"配额 199/日"当成了事实，日志轮转后再也无法复现。

所以这里记录的是**调用本身**，不是日志文本。

429 / 配额证据
--------------
`error_body` 会完整留存上游返回（截断到 2KB）。配额耗尽时上游通常在报错体里
带"已用/上限"，这是拿到真实配额数字唯一可靠的途径——它不能预约，
只能等它自然发生时被捕获。

设计约束
--------
- **独立库**，不写认证库：认证库是 66MB 且承载登录态，
  不该被高频指标写入干扰。
- **埋点失败绝不影响主流程**：任何异常都吞掉，宁可丢一条指标也不能拖垮 LLM 调用。
- **WAL + busy_timeout**：172 上是 4 个 uvicorn worker 并发写同一文件。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 上游报错体留存上限。429 的配额信息通常在开头，2KB 足够，
# 又不至于让单条记录膨胀。
_ERROR_BODY_MAX = 2000
_PROMPT_PREVIEW_MAX = 200

_lock = threading.Lock()
_initialized = False


def _db_path() -> Path:
    root = os.environ.get("AITICKET_DATA_ROOT")
    base = Path(root) if root else Path(__file__).resolve().parent / "data"
    return base / "metrics" / "llm_calls.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")       # 多 worker 并发写
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _initialized
    if _initialized:
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_calls (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT NOT NULL,
            day            TEXT NOT NULL,
            provider       TEXT,
            model          TEXT,
            feature        TEXT,
            caller         TEXT,
            status         TEXT NOT NULL,
            latency_ms     INTEGER,
            prompt_chars   INTEGER,
            response_chars INTEGER,
            error_type     TEXT,
            error_body     TEXT,
            prompt_preview TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_llm_calls_day ON llm_calls(day);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_calls(day, status);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_feature ON llm_calls(day, feature);
        """
    )
    conn.commit()
    _initialized = True


def caller_module(depth: int = 3) -> str:
    """推断调用方模块名，作为 feature 的兜底。

    调用方遍布各处，逐个改签名传 feature 成本高且容易漏；
    先用调用栈自动归因，需要更细粒度时再由调用方显式传 feature。
    """
    try:
        frame = sys._getframe(depth)
    except (ValueError, AttributeError):
        return ""
    name = frame.f_globals.get("__name__", "") if frame else ""
    return str(name)[:80]


def record(
    provider: str = "",
    model: str = "",
    feature: str = "",
    status: str = "ok",
    latency_ms: int = 0,
    prompt: str = "",
    response: str = "",
    error: Optional[BaseException] = None,
    error_body: str = "",
    caller: str = "",
) -> None:
    """记录一次 LLM 调用。**任何异常都被吞掉**——埋点不能拖垮主流程。"""
    try:
        now = datetime.now(timezone.utc)
        body = error_body or (str(error) if error is not None else "")
        row = (
            now.isoformat(timespec="seconds"),
            now.strftime("%Y-%m-%d"),
            str(provider or "")[:40],
            str(model or "")[:80],
            str(feature or "")[:60],
            str(caller or "")[:80],
            str(status or "")[:20],
            int(latency_ms or 0),
            len(prompt or ""),
            len(response or ""),
            (type(error).__name__ if error is not None else "")[:60],
            body[:_ERROR_BODY_MAX],
            (prompt or "")[:_PROMPT_PREVIEW_MAX],
        )
        with _lock:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute(
                    """INSERT INTO llm_calls
                       (ts, day, provider, model, feature, caller, status, latency_ms,
                        prompt_chars, response_chars, error_type, error_body, prompt_preview)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def summary(days: int = 7) -> dict[str, Any]:
    """按天/功能/状态聚合。给"每天调了多少次、哪个功能吃掉多少"一个直接答案。"""
    try:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.row_factory = sqlite3.Row
            by_day = [
                dict(r)
                for r in conn.execute(
                    """SELECT day, COUNT(*) calls,
                              SUM(status='ok') ok,
                              SUM(status!='ok') failed,
                              CAST(AVG(latency_ms) AS INTEGER) avg_ms
                       FROM llm_calls
                       WHERE day >= date('now', ?)
                       GROUP BY day ORDER BY day DESC""",
                    (f"-{int(days)} days",),
                )
            ]
            by_feature = [
                dict(r)
                for r in conn.execute(
                    """SELECT COALESCE(NULLIF(feature,''), caller, '(未归因)') feature,
                              COUNT(*) calls, SUM(status!='ok') failed
                       FROM llm_calls
                       WHERE day >= date('now', ?)
                       GROUP BY 1 ORDER BY calls DESC LIMIT 20""",
                    (f"-{int(days)} days",),
                )
            ]
            errors = [
                dict(r)
                for r in conn.execute(
                    """SELECT ts, provider, error_type, substr(error_body,1,300) error_body
                       FROM llm_calls WHERE status!='ok'
                       ORDER BY id DESC LIMIT 20"""
                )
            ]
            total = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
            return {
                "db_path": str(_db_path()),
                "total_records": total,
                "by_day": by_day,
                "by_feature": by_feature,
                "recent_errors": errors,
            }
        finally:
            conn.close()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "db_path": str(_db_path())}
