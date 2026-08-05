"""集中式日志配置：三流拆分 + trace_id 贯穿 + 消除盲区。

单独成模块（而非塞在 main.py 里），是因为 main.py 一 import 就会拉起
Chroma、后台线程和 LLM 客户端，纯配置逻辑没理由陪绑、也没法单测。

解决的三个实测问题（数据来自 172 生产）：

1. **信号被噪音淹没**：3h49m 产生 33,096 行，99.1% 是 INFO 访问流水
   （`Response: status=… time=…ms` 占 45%），全窗口只有 2 条 ERROR；
   main.log 5 个轮转文件约 4 小时写满 → 排障时现场往往已被冲掉。
   → 拆成 main / access / error 三流，error 单独长留存。

2. **启动崩溃无痕迹**：原先把 uvicorn.error 的 handlers 清空且 propagate=False，
   导致 import 失败的栈只在容器 stdout。当年 /api/agents/* 全 404 难查就是这个原因。
   → 只静音 uvicorn.access（流水已由 access.log 承担），uvicorn.error 必须留声。

3. **模块日志进黑洞**：此前只有 'ai_ticket' 配了 handler，各模块
   logging.getLogger(__name__) 冒泡到无 handler 的 root，等于没写。
   → 给 root 挂 handler。

只依赖标准库。
"""
from __future__ import annotations

import contextvars
import logging
import logging.handlers
import os
import secrets
import time
from pathlib import Path
from typing import Optional

APP_LOGGER_NAME = "ai_ticket"
ACCESS_LOGGER_NAME = "ai_ticket.access"

# 访问流水量大、价值低 → 少留几份；错误量小、价值高 → 多留几份，保证排障时历史还在。
_MAIN_MAX_BYTES = 10 * 1024 * 1024
_MAIN_BACKUPS = 5
_ACCESS_MAX_BYTES = 20 * 1024 * 1024
_ACCESS_BACKUPS = 3
_ERROR_MAX_BYTES = 5 * 1024 * 1024
_ERROR_BACKUPS = 20

# 给 root 挂 handler 后，三方库的 INFO 也会落盘。httpx 每次出站请求打一条 INFO——
# 生产上每次调 Jira/LLM 都刷一行，等于把刚清掉的噪音换个来源请回来
# （172 实测：一次测试运行就产生 168 行 httpx INFO）。
# 这些压到 WARNING：真出问题时仍留声，日常不刷屏。
_NOISY_THIRD_PARTY = (
    "httpx", "httpcore", "urllib3", "requests",
    "chromadb", "sentence_transformers", "transformers",
    "openai", "anthropic", "asyncio", "watchfiles", "PIL", "matplotlib",
)

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(trace_id)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 请求级 trace：contextvar 在 asyncio 下天然按请求隔离
_trace_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("aiticket_trace_id", default=None)

_configured = False


# ──────────────────────────── trace_id ────────────────────────────

def new_trace_id(prefix: str = "rp") -> str:
    """生成一个 trace_id。格式 rp-<毫秒时间戳16进制>-<随机>，短且可按时间排序。"""
    return f"{prefix}-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"


def set_trace_id(trace_id: Optional[str]):
    return _trace_ctx.set(trace_id)


def current_trace_id() -> Optional[str]:
    return _trace_ctx.get()


class _TraceIdFilter(logging.Filter):
    """把当前 trace 注入每条记录。

    没有请求上下文时（启动期、后台线程）必须给个占位符 ——
    否则 format 字符串里的 %(trace_id)s 会抛 KeyError，把日志本身搞挂。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id") or not record.trace_id:
            record.trace_id = _trace_ctx.get() or "-"
        return True


# ──────────────────────────── 目录解析 ────────────────────────────

def resolve_log_dir() -> Path:
    """决定日志落点，按优先级：

    1. AITICKET_LOG_DIR（显式覆盖）
    2. /app/logs —— 容器里 compose 挂的独立卷。让日志与代码目录解耦，
       不再躺在 bind-mount 的仓库目录里靠 .gitignore 兜底。
    3. APP/backend/logs —— 非容器的本机开发场景
    """
    env = os.environ.get("AITICKET_LOG_DIR", "").strip()
    if env:
        return Path(env)

    mounted = Path("/app/logs")
    if mounted.is_dir() and os.access(mounted, os.W_OK):
        return mounted

    return Path(__file__).resolve().parent.parent / "logs"


# ──────────────────────────── 配置入口 ────────────────────────────

def _rotating(path: Path, level: int, fmt: logging.Formatter,
              max_bytes: int, backups: int) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    h.setLevel(level)
    h.setFormatter(fmt)
    h.addFilter(_TraceIdFilter())
    return h


def configure_logging(log_dir: Optional[Path] = None, force: bool = False) -> Path:
    """幂等地装配日志。重复调用不会叠加 handler（否则一行会写多遍）。"""
    global _configured
    if _configured and not force:
        return log_dir or resolve_log_dir()

    log_dir = Path(log_dir) if log_dir else resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)
    trace_filter = _TraceIdFilter()

    main_h = _rotating(log_dir / "main.log", logging.INFO, fmt, _MAIN_MAX_BYTES, _MAIN_BACKUPS)
    error_h = _rotating(log_dir / "error.log", logging.WARNING, fmt, _ERROR_MAX_BYTES, _ERROR_BACKUPS)
    access_h = _rotating(log_dir / "access.log", logging.INFO, fmt, _ACCESS_MAX_BYTES, _ACCESS_BACKUPS)

    console_h = logging.StreamHandler()
    console_h.setFormatter(fmt)
    console_h.addFilter(trace_filter)

    def _reset(lg: logging.Logger):
        for h in list(lg.handlers):
            lg.removeHandler(h)

    # root：兜住所有 logging.getLogger(__name__) 的模块日志
    root = logging.getLogger()
    _reset(root)
    root.setLevel(logging.INFO)
    root.addHandler(main_h)
    root.addHandler(error_h)
    root.addHandler(console_h)
    root.addFilter(trace_filter)

    # 应用日志：显式挂同样的 handler，并停止向 root 冒泡以免写两遍
    app = logging.getLogger(APP_LOGGER_NAME)
    _reset(app)
    app.setLevel(logging.INFO)
    app.propagate = False
    app.addHandler(main_h)
    app.addHandler(error_h)
    app.addHandler(console_h)

    # 访问流水：独立成流，绝不进 main.log
    access = logging.getLogger(ACCESS_LOGGER_NAME)
    _reset(access)
    access.setLevel(logging.INFO)
    access.propagate = False
    access.addHandler(access_h)

    # uvicorn.error 必须留声（当年 404 盲区的根因）；
    # uvicorn.access 静音，因为流水已由我们的 access.log 承担，留着就是重复刷屏。
    uv_err = logging.getLogger("uvicorn.error")
    _reset(uv_err)
    uv_err.setLevel(logging.INFO)
    uv_err.propagate = True          # 冒泡到 root → 落进 main.log / error.log

    uv_acc = logging.getLogger("uvicorn.access")
    _reset(uv_acc)
    uv_acc.propagate = False

    # 三方库降噪：只压级别，不摘 handler —— WARNING 及以上仍会落盘
    for name in _NOISY_THIRD_PARTY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    return log_dir


def get_logger(name: str = APP_LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)
