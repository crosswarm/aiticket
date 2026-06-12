#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily-cache-purge-vacuum 看门狗（铁律：定时任务必须有 检查+自动补救+告警）。

为什么不查 schedule 的 last_status：scheduler_service._execute 的 except 只 log
不置 failed，handler 异常时 last_status 仍记 success（已知静默失败模式）。
本看门狗查【效果】——cache_purge_vacuum.py 成功跑完会写
data/cache_purge_heartbeat.json，心跳超过阈值(默认 26h) 即判定漏跑/失败：
  1) 自动补救：直接子进程重跑 cache_purge_vacuum.py（幂等：逻辑删过期 + VACUUM 可跳过）
  2) 补救后复查心跳；仍失败 → 飞书 webhook 告警（NOTIFICATION_WEBHOOK_URL）
  3) 补救成功也发一条 info 告警，让漏跑本身浮出水面

退出码：0=健康或补救成功；1=补救失败（已告警）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
HEARTBEAT = BACKEND / "data" / "cache_purge_heartbeat.json"
PURGE_SCRIPT = BACKEND / "scripts" / "cache_purge_vacuum.py"
STALE_HOURS = float(os.environ.get("CACHE_PURGE_STALE_HOURS", "26"))
REMEDIATE_TIMEOUT_S = int(os.environ.get("CACHE_PURGE_REMEDIATE_TIMEOUT", "900"))


def _log(msg: str) -> None:
    print(f"[purge-watchdog] {msg}", flush=True)


def _heartbeat_age_hours() -> float | None:
    """返回心跳年龄(小时)；缺失/损坏返回 None。"""
    try:
        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["last_success"])
        return (datetime.now() - ts).total_seconds() / 3600.0
    except Exception:
        return None


def _notify(text: str) -> None:
    """飞书 webhook 告警（fail-soft：无 URL/失败只打日志）。"""
    url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "").strip()
    if not url:
        _log(f"[notify-skip 无 webhook] {text}")
        return
    try:
        import urllib.request
        fmt = os.environ.get("NOTIFICATION_WEBHOOK_FORMAT", "feishu").lower()
        if fmt == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
        else:  # generic / wecom 兼容字段
            payload = {"text": text, "msgtype": "text"}
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        _log("[notify] 已发送告警")
    except Exception as e:
        _log(f"[notify] 发送失败: {e!r}")


def _remediate() -> bool:
    """子进程重跑 purge 脚本，返回是否成功（rc==0 且心跳刷新）。"""
    _log(f"补救：重跑 {PURGE_SCRIPT}")
    try:
        r = subprocess.run([sys.executable, str(PURGE_SCRIPT)],
                           cwd=str(BACKEND), timeout=REMEDIATE_TIMEOUT_S,
                           capture_output=True, text=True)
        tail = (r.stdout or "")[-500:]
        _log(f"补救 rc={r.returncode} 输出尾部:\n{tail}")
        if r.returncode != 0:
            return False
    except Exception as e:
        _log(f"补救子进程异常: {e!r}")
        return False
    age = _heartbeat_age_hours()
    return age is not None and age < 1.0


def main() -> int:
    age = _heartbeat_age_hours()
    host = os.uname().nodename if hasattr(os, "uname") else "unknown"
    if age is not None and age < STALE_HOURS:
        _log(f"健康：心跳 {age:.1f}h 前（阈值 {STALE_HOURS}h）")
        return 0

    desc = "心跳文件缺失" if age is None else f"心跳已 {age:.1f}h 未刷新（阈值 {STALE_HOURS}h）"
    _log(f"异常：{desc}，开始补救")
    if _remediate():
        _notify(f"⚠️ [aiticket@{host}] daily-cache-purge-vacuum 漏跑（{desc}），看门狗已自动补跑成功。请关注调度器为何未执行。")
        _log("补救成功")
        return 0
    _notify(f"🔴 [aiticket@{host}] daily-cache-purge-vacuum 漏跑（{desc}），且看门狗补跑也失败——缓存清理/VACUUM 链路中断，需人工介入。")
    _log("补救失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
