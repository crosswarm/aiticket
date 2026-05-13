"""
SuperGemma4 本地 MLX 模型生命周期管理。

三原则：
1. Preflight — 任务开始前探活，未运行则自动启动
2. Fallback  — 90s 内启不来则降级到 zhipu
3. 随手关灯  — 任务结束（成功/失败/异常）释放本进程启动的实例
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

_LOCAL_URL = "http://127.0.0.1:8090/v1/models"
_VENV_PYTHON = "/Users/cfone/Studio/.venvs/mlx-hb314/bin/python"
_SERVER_SCRIPT = "/Users/cfone/Studio/scripts/_supergemma_server.py"
_MODEL_DIR = "/Users/cfone/Studio/models/supergemma4-26b-uncensored-mlx-4bit-v2"
_PORT = 8090

_CACHE_ROOT = "/Users/cfone/Studio/.cache"
_TMP_DIR = "/Users/cfone/Studio/.tmp"

_PID_FILE = Path.home() / ".gstack" / ".supergemma4-autostarted.pid"
_LOCK_FILE = Path.home() / ".gstack" / ".supergemma4-launching"

_WAIT_SECONDS = 120   # MLX 26B 模型加载最长 60-90s，留余量


# ── 基础检测 ──────────────────────────────────────────────────────────────────

def is_alive(timeout: float = 3.0) -> bool:
    try:
        import requests
        r = requests.get(_LOCAL_URL, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


# ── 启动 ──────────────────────────────────────────────────────────────────────

def _start_server() -> int | None:
    """后台启动 MLX server，返回子进程 pid；失败返回 None。"""
    if not Path(_VENV_PYTHON).exists():
        logger.error("[local_llm] venv python not found: %s", _VENV_PYTHON)
        return None
    if not Path(_MODEL_DIR, "config.json").exists():
        logger.error("[local_llm] model files missing: %s", _MODEL_DIR)
        return None

    env = {
        **os.environ,
        "HF_HOME": f"{_CACHE_ROOT}/huggingface",
        "XDG_CACHE_HOME": f"{_CACHE_ROOT}/xdg",
        "HF_HUB_CACHE": f"{_CACHE_ROOT}/huggingface/hub",
        "HF_ASSETS_CACHE": f"{_CACHE_ROOT}/huggingface/assets",
        "HF_XET_CACHE": f"{_CACHE_ROOT}/huggingface/xet",
        "HF_HUB_DISABLE_XET": "1",
        "TMPDIR": _TMP_DIR,
    }

    log_path = Path(_TMP_DIR) / "supergemma4-autostart.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        _VENV_PYTHON, _SERVER_SCRIPT,
        "--model", _MODEL_DIR,
        "--port", str(_PORT),
        "--host", "127.0.0.1",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=open(log_path, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(proc.pid))
        logger.info("[local_llm] started SuperGemma4 (pid=%d)", proc.pid)
        return proc.pid
    except Exception as exc:
        logger.error("[local_llm] failed to start server: %s", exc)
        return None


# ── 主接口 ────────────────────────────────────────────────────────────────────

def _notify_start_event(msg: str) -> None:
    try:
        from services.feishu_notifier import get_notifier
        get_notifier().send_message(msg)
    except Exception:
        pass


def ensure_running(wait_seconds: int = _WAIT_SECONDS, max_attempts: int = 3) -> bool:
    """
    确保 SuperGemma4 在线：
    ① 已活跃 → True
    ② 未活跃 → 后台启动，最多 max_attempts 次重试，每次轮询 wait_seconds 秒
    ③ 全部失败 → False（飞书告警）

    使用文件锁防止并发重复启动。
    """
    if is_alive():
        return True

    # 另一个进程正在启动 — 等待它完成即可
    if _LOCK_FILE.exists():
        logger.info("[local_llm] another process is launching, waiting…")
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if is_alive():
                return True
            time.sleep(5)
        return False

    _LOCK_FILE.touch()
    _backoffs = [5, 15, 30]
    try:
        for attempt in range(max_attempts):
            if is_alive():
                return True
            pid = _start_server()
            if pid is None:
                # 脚本/模型缺失，重试没有意义
                _notify_start_event(
                    f"🚨 SuperGemma4 无法启动：脚本或模型文件缺失\n"
                    f"路径：{_SERVER_SCRIPT}\n模型：{_MODEL_DIR}"
                )
                return False
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                if is_alive():
                    logger.info("[local_llm] SuperGemma4 healthy (attempt %d/%d)", attempt + 1, max_attempts)
                    return True
                time.sleep(5)
            logger.warning("[local_llm] attempt %d/%d: not healthy within %ds", attempt + 1, max_attempts, wait_seconds)
            _notify_start_event(
                f"⚠️ SuperGemma4 自启第 {attempt + 1}/{max_attempts} 次失败（{wait_seconds}s 内未就绪）\n"
                f"日志：{_TMP_DIR}/supergemma4-autostart.log"
            )
            if attempt < max_attempts - 1:
                time.sleep(_backoffs[attempt])
        logger.error("[local_llm] SuperGemma4 failed after %d attempts", max_attempts)
        return False
    finally:
        _LOCK_FILE.unlink(missing_ok=True)


def with_fallback(task_name: str) -> Literal["local", "zhipu", "minimax"]:
    """
    Task handler 入口调用：
    - 返回 "local"  → SuperGemma4 可用
    - 返回 "zhipu"  → 降级（自动飞书告警）
    """
    if ensure_running():
        return "local"

    logger.warning("[local_llm] %s: SuperGemma4 unavailable, fallback zhipu", task_name)
    _notify_start_event(
        f"⚠️ [{task_name}] SuperGemma4 三次自启均失败，已降级到 zhipu\n"
        f"日志：{_TMP_DIR}/supergemma4-autostart.log"
    )
    return "zhipu"


def with_fallback_chain(task_name: str, chain: list = None) -> str:
    """
    按 chain 顺序返回首个可用 provider。
    local 触发 ensure_running（最多 3 次重试），失败后尝试链中下一个。
    """
    if chain is None:
        chain = ["local", "zhipu", "minimax"]
    for provider in chain:
        if provider == "local":
            if ensure_running(max_attempts=3):
                return "local"
            logger.warning("[local_llm] %s: local unavailable, trying next in chain", task_name)
            continue
        # 非本地 provider 直接返回（外部 LLM 失败时由 llm_service 自己重试）
        return provider
    # chain 全部失败（理论上不应到达这里，除非 chain 全是 "local"）
    _notify_start_event(
        f"🚨 [{task_name}] 所有 LLM provider 均不可用 chain={chain}，任务将以末尾 provider 尝试"
    )
    return chain[-1] if chain else "minimax"


def daytime_chain(task_name: str) -> list:
    """
    返回随时间窗动态调整的 fallback chain。
    白天 (09-21 本地时间): 在线优先，避免 SuperGemma4 冷启探活浪费约 1 分钟。
    夜间: local 优先，节省 GPU 成本。
    """
    from datetime import datetime as _dt
    hour = _dt.now().hour
    if 9 <= hour < 21:
        chain = ["minimax", "zhipu", "local"]
    else:
        chain = ["local", "zhipu", "minimax"]
    logger.debug("[local_llm] %s daytime_chain hour=%d → %s", task_name, hour, chain)
    return chain


from contextlib import contextmanager


@contextmanager
def lifecycle(task_name: str, *, required: bool = True):
    """探活 → 自启（必要时）→ yield → finally 关灯。

    用法：
        with lifecycle("nightly_training", required=False) as provider:
            run_with_provider(provider)  # provider = "local" | "zhipu"

    required=True（默认）：local 不可用 → RuntimeError
    required=False：local 不可用 → yield "zhipu"，调用方自行降级
    """
    started_by_us_before = _PID_FILE.exists()
    ok = ensure_running()
    started_by_us_now = _PID_FILE.exists() and not started_by_us_before
    try:
        if ok:
            yield "local"
        elif required:
            raise RuntimeError(f"[{task_name}] SuperGemma4 不可用，且 required=True")
        else:
            yield "zhipu"
    finally:
        if started_by_us_now:
            shutdown_if_started_by_us(task_name)


def shutdown_if_started_by_us(task_name: str) -> None:
    """
    随手关灯：仅关闭由本进程（通过 ensure_running）启动的 MLX server。
    用户手动启动的实例（_PID_FILE 不存在）不会被误关。
    """
    if not _PID_FILE.exists():
        return
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)
        logger.info("[local_llm] shutdown SuperGemma4 (pid=%d) after %s", pid, task_name)
    except ProcessLookupError:
        _PID_FILE.unlink(missing_ok=True)
        logger.debug("[local_llm] SuperGemma4 pid=%s already gone", pid)
    except (ValueError, OSError) as exc:
        logger.warning("[local_llm] shutdown error: %s", exc)
        _PID_FILE.unlink(missing_ok=True)
