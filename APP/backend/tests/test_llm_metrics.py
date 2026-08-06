"""LLM 调用埋点测试。

最关键的两条：
1. **429 的上游报错体必须被完整留存**——配额数字只藏在那里，且不能预约，
   只能等它自然发生时被捕获。历史上正因为拿不到它，"配额 199/日"
   一直是个无法复现的一次性观测。
2. **埋点失败绝不能拖垮 LLM 调用**——宁可丢指标，不能丢回复。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_metrics  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AITICKET_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(llm_metrics, "_initialized", False)
    yield


def _rows():
    import sqlite3
    conn = sqlite3.connect(llm_metrics._db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id")]
    finally:
        conn.close()


# ---------------------------------------------------------------- 基本记录


def test_records_successful_call():
    llm_metrics.record(provider="deepseek", model="deepseek-v4-flash", feature="smart_reply",
                       status="ok", latency_ms=1234, prompt="问题", response="答复内容")
    row = _rows()[0]
    assert row["provider"] == "deepseek"
    assert row["feature"] == "smart_reply"
    assert row["status"] == "ok"
    assert row["latency_ms"] == 1234
    assert row["prompt_chars"] == 2
    assert row["response_chars"] == 4
    assert row["day"] and row["ts"]


def test_counts_calls_not_log_lines():
    """一次调用一条记录——历史错误正是把日志行数当调用数（3.9 vs 实际 1.9）。"""
    for _ in range(5):
        llm_metrics.record(provider="deepseek", status="ok")
    assert len(_rows()) == 5
    assert llm_metrics.summary()["total_records"] == 5


# ---------------------------------------------------------------- 429 证据留存


def test_quota_error_body_is_preserved():
    """核心用例：配额耗尽时上游报的"已用/上限"必须完整落库。"""
    body = 'status_code=429 | body={"error":{"message":"quota exceeded: 203/199 per day"}}'
    llm_metrics.record(provider="deepseek", status="error",
                       error=RuntimeError("429 Too Many Requests"), error_body=body)
    row = _rows()[0]
    assert row["status"] == "error"
    assert row["error_type"] == "RuntimeError"
    assert "203/199" in row["error_body"], "配额数字丢失，这条记录就失去意义"


def test_error_body_truncated_but_keeps_head():
    """报错体截断，但配额信息通常在开头，必须保住。"""
    body = "quota exceeded: 203/199" + "x" * 9000
    llm_metrics.record(status="error", error_body=body)
    stored = _rows()[0]["error_body"]
    assert len(stored) <= llm_metrics._ERROR_BODY_MAX
    assert "203/199" in stored


def test_recent_errors_surfaced_in_summary():
    llm_metrics.record(status="ok")
    llm_metrics.record(status="error", error=ValueError("boom"), error_body="quota 203/199")
    errors = llm_metrics.summary()["recent_errors"]
    assert len(errors) == 1
    assert "203/199" in errors[0]["error_body"]


# ---------------------------------------------------------------- 归因


def test_caller_module_is_recorded_when_feature_absent():
    llm_metrics.record(status="ok", caller="services.nightly_precompute")
    feats = {f["feature"] for f in llm_metrics.summary()["by_feature"]}
    assert "services.nightly_precompute" in feats


def test_feature_wins_over_caller():
    llm_metrics.record(status="ok", feature="smart_reply", caller="services.x")
    assert llm_metrics.summary()["by_feature"][0]["feature"] == "smart_reply"


def test_caller_module_returns_string():
    assert isinstance(llm_metrics.caller_module(), str)


# ---------------------------------------------------------------- 健壮性


def test_record_never_raises_on_bad_input():
    """埋点不能拖垮主流程——任何输入都不许抛。"""
    llm_metrics.record(provider=None, model=None, latency_ms=None, prompt=None, response=None)
    llm_metrics.record(status="ok", latency_ms="not-an-int")
    llm_metrics.record(error=RuntimeError("x"))


def test_record_never_raises_when_db_unwritable(monkeypatch, tmp_path):
    bad = tmp_path / "nope"
    bad.write_text("not a directory")
    monkeypatch.setattr(llm_metrics, "_db_path", lambda: bad / "sub" / "x.db")
    llm_metrics.record(status="ok")   # 不许抛


def test_summary_reports_error_instead_of_raising(monkeypatch, tmp_path):
    bad = tmp_path / "nope2"
    bad.write_text("not a directory")
    monkeypatch.setattr(llm_metrics, "_db_path", lambda: bad / "sub" / "x.db")
    out = llm_metrics.summary()
    assert "error" in out


def test_summary_on_empty_db():
    out = llm_metrics.summary()
    assert out["total_records"] == 0
    assert out["by_day"] == [] and out["recent_errors"] == []


def test_metrics_db_is_separate_from_auth_db():
    """指标高频写入不能落到认证库（66MB，承载登录态）。"""
    llm_metrics.record(status="ok")
    p = str(llm_metrics._db_path())
    assert p.endswith("metrics/llm_calls.db")
    assert "app_auth" not in p


# ---------------------------------------------------------------- 与 call_llm 的集成


class _FakeResponse:
    text = '{"error":{"message":"quota exceeded: 203/199 per day","type":"rate_limit"}}'


class _Quota429(Exception):
    status_code = 429
    response = _FakeResponse()


def _llm_service():
    import llm_service
    return llm_service


def test_call_llm_captures_429_body(monkeypatch):
    """端到端：上游抛 429 时，配额原文必须落进指标库。

    这是拿到真实配额数字的唯一途径——429 不能预约，
    只能等它自然发生时被捕获。
    """
    ls = _llm_service()
    monkeypatch.setattr(ls, "_metrics", llm_metrics)

    def boom(*a, **kw):
        raise _Quota429("429 Too Many Requests")

    monkeypatch.setattr(ls.LLMService, "_call_openai_with_retry", boom)
    svc = ls.LLMService()
    with pytest.raises(_Quota429):
        svc.call_llm("问题", api_key="k", provider="deepseek", model_name="m", feature="smart_reply")

    row = _rows()[0]
    assert row["status"] == "error"
    assert row["feature"] == "smart_reply"
    assert "203/199" in row["error_body"], "429 配额原文没被留存，埋点失去意义"
    assert "status_code=429" in row["error_body"]


def test_call_llm_records_success(monkeypatch):
    ls = _llm_service()
    monkeypatch.setattr(ls, "_metrics", llm_metrics)
    monkeypatch.setattr(ls.LLMService, "_call_openai_with_retry",
                        lambda *a, **kw: iter(["答", "复"]))
    svc = ls.LLMService()
    out = svc.call_llm("问题", api_key="k", provider="deepseek", model_name="m", feature="smart_reply")
    assert out == "答复"
    row = _rows()[0]
    assert row["status"] == "ok" and row["response_chars"] == 2
    assert row["latency_ms"] >= 0


def test_call_llm_without_key_is_recorded(monkeypatch):
    ls = _llm_service()
    monkeypatch.setattr(ls, "_metrics", llm_metrics)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    svc = ls.LLMService()
    out = svc.call_llm("问题", api_key="", provider="deepseek")
    assert out.startswith("Error: No API Key")
    assert _rows()[0]["status"] == "no_api_key"


def test_call_llm_error_still_propagates(monkeypatch):
    """埋点不能改变原有行为：异常照样上抛。"""
    ls = _llm_service()
    monkeypatch.setattr(ls, "_metrics", llm_metrics)

    def boom(*a, **kw):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(ls.LLMService, "_call_openai_with_retry", boom)
    with pytest.raises(RuntimeError, match="upstream down"):
        ls.LLMService().call_llm("q", api_key="k", provider="deepseek")
