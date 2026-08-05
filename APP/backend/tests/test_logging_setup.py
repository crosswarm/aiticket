"""日志体系的行为守卫。

锁死几条【踩过坑才总结出来】的性质，防止回退：
1. uvicorn.error 不能被静音 —— 当年 /api/agents/* 全 404 排查困难，
   就是因为启动期 import 崩溃只进 uvicorn.error，而它 handlers 被清空了。
2. root logger 必须有 handler —— 否则各模块 logging.getLogger(__name__)
   的输出进黑洞（[BadcaseMark] float32 那条告警就只在 docker logs 里）。
3. 访问流水必须与应用日志分流 —— 实测 172 上 99.1% 的行是访问流水，
   信号被彻底淹没。
4. trace_id 必须能贯穿，且在没有请求上下文时也不能让日志格式化炸掉。
"""
from __future__ import annotations

import logging
from pathlib import Path

from services.logging_setup import (
    ACCESS_LOGGER_NAME,
    APP_LOGGER_NAME,
    configure_logging,
    current_trace_id,
    new_trace_id,
    resolve_log_dir,
    set_trace_id,
)


def _reset():
    """把全局 logging 状态复位，避免用例间互相污染。"""
    for name in ("", APP_LOGGER_NAME, ACCESS_LOGGER_NAME, "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        lg.filters.clear()


def test_creates_three_streams(tmp_path):
    _reset()
    configure_logging(log_dir=tmp_path, force=True)
    logging.getLogger(APP_LOGGER_NAME).info("业务里程碑")
    logging.getLogger(APP_LOGGER_NAME).warning("出事了")
    logging.getLogger(ACCESS_LOGGER_NAME).info("GET /api/x 200 12ms")
    for h in logging.getLogger(APP_LOGGER_NAME).handlers + logging.getLogger(ACCESS_LOGGER_NAME).handlers:
        h.flush()

    main_txt = (tmp_path / "main.log").read_text(encoding="utf-8")
    access_txt = (tmp_path / "access.log").read_text(encoding="utf-8")
    error_txt = (tmp_path / "error.log").read_text(encoding="utf-8")

    assert "业务里程碑" in main_txt
    # 访问流水【不能】混进 main.log —— 这正是 99.1% 噪音的来源
    assert "GET /api/x" not in main_txt
    assert "GET /api/x" in access_txt
    # error.log 只收 WARNING+
    assert "出事了" in error_txt
    assert "业务里程碑" not in error_txt


def test_uvicorn_error_is_not_silenced(tmp_path):
    """当年 404 盲区的守卫：uvicorn.error 必须仍能落盘。"""
    _reset()
    configure_logging(log_dir=tmp_path, force=True)
    logging.getLogger("uvicorn.error").error("Traceback: ModuleNotFoundError: no_such_mod")
    for h in logging.getLogger("uvicorn.error").handlers:
        h.flush()
    for h in logging.getLogger().handlers:
        h.flush()

    assert "no_such_mod" in (tmp_path / "main.log").read_text(encoding="utf-8")


def test_module_level_logger_reaches_disk(tmp_path):
    """任意模块的 getLogger(__name__) 都要落盘，而不是进黑洞。"""
    _reset()
    configure_logging(log_dir=tmp_path, force=True)
    logging.getLogger("services.reply_reuse_evaluator").warning("[NegativePair] 写入失败")
    for h in logging.getLogger().handlers:
        h.flush()

    assert "[NegativePair] 写入失败" in (tmp_path / "main.log").read_text(encoding="utf-8")


def test_trace_id_flows_into_records(tmp_path):
    _reset()
    configure_logging(log_dir=tmp_path, force=True)
    tid = new_trace_id()
    set_trace_id(tid)
    assert current_trace_id() == tid
    logging.getLogger(APP_LOGGER_NAME).info("带 trace 的一行")
    for h in logging.getLogger(APP_LOGGER_NAME).handlers:
        h.flush()

    assert tid in (tmp_path / "main.log").read_text(encoding="utf-8")


def test_no_trace_context_does_not_break_formatting(tmp_path):
    """后台线程/启动期没有请求上下文，日志也必须能正常写出去。"""
    _reset()
    set_trace_id(None)
    configure_logging(log_dir=tmp_path, force=True)
    logging.getLogger(APP_LOGGER_NAME).info("无 trace 上下文")
    for h in logging.getLogger(APP_LOGGER_NAME).handlers:
        h.flush()

    assert "无 trace 上下文" in (tmp_path / "main.log").read_text(encoding="utf-8")


def test_configure_is_idempotent(tmp_path):
    """重复 import/调用不应叠加 handler（否则日志一行写多遍）。"""
    _reset()
    configure_logging(log_dir=tmp_path, force=True)
    n1 = len(logging.getLogger(APP_LOGGER_NAME).handlers)
    configure_logging(log_dir=tmp_path)
    configure_logging(log_dir=tmp_path)
    assert len(logging.getLogger(APP_LOGGER_NAME).handlers) == n1


def test_chatty_third_party_loggers_are_quieted(tmp_path):
    """★ 给 root 挂 handler 后，三方库的 INFO 也会落盘。

    httpx 每次出站请求都打一条 INFO —— 生产上每次调 Jira/LLM 都刷一行，
    等于把刚清掉的噪音又换个来源请回来。实测 172 上一次测试运行就产生了
    168 行 httpx INFO。这些库必须压到 WARNING。
    """
    _reset()
    configure_logging(log_dir=tmp_path, force=True)

    logging.getLogger("httpx").info("HTTP Request: GET http://x 200 OK")
    logging.getLogger("httpcore").info("connect_tcp.started")
    logging.getLogger("urllib3.connectionpool").info("Starting new HTTPS connection")
    for h in logging.getLogger().handlers:
        h.flush()

    txt = (tmp_path / "main.log").read_text(encoding="utf-8")
    assert "HTTP Request" not in txt
    assert "connect_tcp" not in txt
    assert "Starting new HTTPS connection" not in txt

    # 但它们的 WARNING/ERROR 仍要留声，否则真出网络问题就瞎了
    logging.getLogger("httpx").warning("连接池耗尽")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "连接池耗尽" in (tmp_path / "main.log").read_text(encoding="utf-8")


def test_resolve_log_dir_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITICKET_LOG_DIR", str(tmp_path / "custom"))
    assert resolve_log_dir() == Path(tmp_path / "custom")


def test_resolve_log_dir_falls_back_when_env_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("AITICKET_LOG_DIR", raising=False)
    d = resolve_log_dir()
    assert isinstance(d, Path)
    assert d.name == "logs"
