"""
本机 jobmaster daemon。

职责：
  1. 运行 scheduler_service cron（所有 task handler）
  2. 运行 pm_scheduler（协作任务看板保鲜）
  3. 运行 automation poll loop（自动化规则）
  4. 运行 kb_write_dispatcher（KB 写操作队列）

API workers（uvicorn）不设 RUN_BACKGROUND_JOBS=1，不再 in-process 跑这些，
避免多 worker 重复触发 cron 和并发写 DB。
"""

import os
import sys
import signal
import time
import threading
import logging
import subprocess
import fcntl
import atexit
from pathlib import Path

# 确保 backend 目录在 sys.path 首位
_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

# 单例锁：防止 LaunchAgent 重启 / 误手动启动产生孤儿 daemon 双发飞书
_LOCK_PATH = _BACKEND / "logs" / ".local_jobmaster.lock"
_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
_lock_fp = open(_LOCK_PATH, "w")
try:
    fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.stderr.write(f"[jm-daemon] 已有实例运行 (lock={_LOCK_PATH})，退出\n")
    sys.exit(0)
_lock_fp.write(str(os.getpid()))
_lock_fp.flush()

@atexit.register
def _release_lock():
    try:
        fcntl.flock(_lock_fp, fcntl.LOCK_UN)
        _lock_fp.close()
        _LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass

os.environ["RUN_BACKGROUND_JOBS"] = "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [jm-daemon] %(message)s",
)
logger = logging.getLogger(__name__)

_PYTHON = "/Volumes/MacMini/opt/miniconda3/envs/antigravity/bin/python3.12"
_JM_SCRIPT = str(_BACKEND / "scripts" / "jobmaster_agent.py")
_JM_CWD = str(_BACKEND)
_PROJ_ROOT = str(_BACKEND.parent.parent)


# ─── 通用工具 ─────────────────────────────────────────────────────────────────

def _load_playbook() -> str | None:
    """每 tick 读一次 jobmaster_playbook.md（<8KB）。pending 不读，强制走人工 approve。"""
    p = _BACKEND / "data" / "jobmaster_playbook.md"
    if p.exists():
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")[:8192]
            logger.debug(f"[JobMaster] playbook loaded: {len(content)} chars")
            return content
        except Exception:
            return None
    return None


def _recover_orphaned_tasks() -> None:
    """daemon 启动时扫描 running 超时任务，强制置 FAILED（防永久 stuck）。"""
    _THRESHOLDS = {
        "daily_summary": 15, "nightly_exploration": 240,
    }
    _DEFAULT_MINUTES = 120
    try:
        from services.agent_task_store import AgentTaskStore
        from agents.base import AgentStatus
        from datetime import datetime, timezone, timedelta
        store = AgentTaskStore.get_instance()
        cleaned = []
        for t in store.list_recent(status="running", limit=200):
            minutes = _THRESHOLDS.get(t.agent_name, _DEFAULT_MINUTES)
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            started = t.started_at
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started and started < cutoff:
                import json as _j
                result = _j.dumps({"error": "daemon_restart_recovery",
                                   "error_kind": "daemon_restart_recovery",
                                   "elapsed_min": int((datetime.now(timezone.utc) - started).total_seconds() / 60)})
                store.update_status(t.id, AgentStatus.FAILED, result_json=result)
                cleaned.append(f"{t.agent_name}:{t.id[:12]} ({minutes}min threshold)")
        if cleaned:
            logger.warning(f"[orphan-recovery] cleaned {len(cleaned)} stuck tasks: {', '.join(cleaned)}")
            try:
                _notifier().send_message(
                    f"♻️ JobMaster 启动孤儿恢复：清理 {len(cleaned)} 个卡死任务\n" +
                    "\n".join(f"  • {c}" for c in cleaned)
                )
            except Exception:
                pass
        else:
            logger.info("[orphan-recovery] no stuck tasks found")
    except Exception as e:
        logger.error(f"[orphan-recovery] failed: {e}")


def _notifier():
    from services.feishu_notifier import get_notifier
    return get_notifier()


def _llm():
    from llm_service import LLMService
    return LLMService()


# ─── Task Handlers ────────────────────────────────────────────────────────────

def _task_weekly_report(notify_on_complete: bool = True, project_key: str = "MYPROJECT",
                        domain_modules=None, **kwargs):
    notifier = _notifier()
    _modules_arg = ["--modules", ",".join(domain_modules)] if domain_modules else []
    try:
        notifier.send_message(f"📊 {project_key} 周报生成已触发，后台执行中...")
        result = subprocess.run(
            [_PYTHON, str(_BACKEND / "scripts" / "run_weekly_report.py"), "--project", project_key] + _modules_arg,
            capture_output=True, text=True, timeout=300, cwd=_PROJ_ROOT,
        )
        if result.returncode == 0:
            if notify_on_complete:
                notifier.send_message(f"📊 {project_key} 周报生成完成\n" + (result.stdout[-200:] if result.stdout else ""))
            logger.info(f"[task:weekly_report:{project_key}] done")
        else:
            notifier.send_message(f"❌ {project_key} 周报生成失败\n{result.stderr[-200:]}")
            logger.error(f"[task:weekly_report:{project_key}] failed: {result.stderr[:300]}")
    except Exception as e:
        logger.error(f"[task:weekly_report:{project_key}] {e}")
        try:
            notifier.send_message(f"❌ {project_key} 周报生成异常: {e}")
        except Exception:
            pass


def _task_monthly_report(notify_on_complete: bool = True, project_key: str = "MYPROJECT",
                         domain_modules=None, **kwargs):
    notifier = _notifier()
    _modules_arg = ["--modules", ",".join(domain_modules)] if domain_modules else []
    try:
        notifier.send_message(f"📊 {project_key} 月报生成已触发，后台执行中...")
        result = subprocess.run(
            [_PYTHON, str(_BACKEND / "scripts" / "run_monthly_report.py"), "--force", "--project", project_key] + _modules_arg,
            capture_output=True, text=True, timeout=600, cwd=_PROJ_ROOT,
        )
        if result.returncode == 0:
            if notify_on_complete:
                notifier.send_message(f"📊 {project_key} 月报生成完成\n" + (result.stdout[-200:] if result.stdout else ""))
            logger.info(f"[task:monthly_report:{project_key}] done")
        else:
            notifier.send_message(f"❌ {project_key} 月报生成失败\n{result.stderr[-200:]}")
            logger.error(f"[task:monthly_report:{project_key}] failed: {result.stderr[:300]}")
    except Exception as e:
        logger.error(f"[task:monthly_report:{project_key}] {e}")
        try:
            notifier.send_message(f"❌ {project_key} 月报生成异常: {e}")
        except Exception:
            pass


def _task_nightly_exploration(stop_hour: int = 7, notify_on_complete: bool = True, **kwargs):
    notifier = _notifier()
    _script = str(_BACKEND / "scripts" / "exploration_agent.py")

    sys.path.insert(0, str(_BACKEND))
    try:
        from services.local_llm_lifecycle import with_fallback as _with_fallback, shutdown_if_started_by_us as _shutdown
        provider = _with_fallback("nightly_exploration")
    except Exception as _exc:
        logger.warning("[task:nightly_exploration] lifecycle import failed: %s", _exc)
        provider = "zhipu"
        _shutdown = None

    _cmd = [_PYTHON, _script]
    _env = {**os.environ, "EXPLORATION_STOP_HOUR": str(stop_hour), "LLM_PROVIDER_OVERRIDE": provider}

    last_stderr = ""
    last_rc = -1
    try:
        for attempt in range(1, 3):
            try:
                result = subprocess.run(
                    _cmd, capture_output=True, text=True,
                    timeout=6 * 3600, cwd=_PROJ_ROOT, env=_env,
                )
                last_rc = result.returncode
                last_stderr = result.stderr
                if result.returncode == 0:
                    logger.info(f"[task:nightly_exploration] done (attempt {attempt})")
                    if notify_on_complete:
                        notifier.send_message("✅ 夜间探索完成")
                    return
                logger.error(f"[task:nightly_exploration] attempt {attempt} failed (rc={result.returncode}): {result.stderr[:300]}")
            except Exception as exc:
                last_stderr = str(exc)
                last_rc = -1
                logger.error(f"[task:nightly_exploration] attempt {attempt} exception: {exc}")

            if attempt < 2:
                logger.info("[task:nightly_exploration] 等待 5 分钟后重试...")
                time.sleep(300)

        try:
            diagnosis = _llm().call_llm(
                f"以下是夜间自动探索任务的失败输出（exit code={last_rc}），请用中文给出根因判断和建议修复步骤，简明扼要50字内：\n\n{last_stderr[:800]}",
                provider="zhipu",
            )
        except Exception:
            diagnosis = "（LLM诊断不可用）"
        notifier.send_message(
            f"❌ 夜间探索失败（重试2次均失败）\n"
            f"**错误**：{last_stderr[:400]}\n"
            f"**诊断**：{diagnosis}"
        )
    finally:
        if _shutdown:
            _shutdown("nightly_exploration")


def _task_reply_training(questions: int = 300, stop_hour: int = 7,
                         run_backfill: bool = False, backfill_limit: int = 50, **kwargs):
    notifier = _notifier()
    _script = str(_BACKEND / "scripts" / "reply_optimization_trainer.py")

    sys.path.insert(0, str(_BACKEND))
    try:
        from services.local_llm_lifecycle import with_fallback as _with_fallback, shutdown_if_started_by_us as _shutdown
        provider = _with_fallback("reply_training")
    except Exception as _exc:
        logger.warning("[task:reply_training] lifecycle import failed: %s", _exc)
        provider = "zhipu"
        _shutdown = None

    _env = {**os.environ, "QCL_BACKEND_URL": "http://ticket.spux.cn", "LLM_PROVIDER_OVERRIDE": provider}
    try:
        try:
            result = subprocess.run(
                [_PYTHON, _script, "--questions", str(questions), "--stop-hour", str(stop_hour), "--resume"],
                capture_output=True, text=True, timeout=4 * 3600, cwd=_PROJ_ROOT, env=_env,
            )
            if result.returncode == 0:
                # 语义校验：检查训练是否真正学到了数据
                _metrics_path = _PROJ_ROOT / "conclusion" / "_local" / "training" / "training_metrics.jsonl"
                _n = 0
                try:
                    lines = _metrics_path.read_text(encoding="utf-8").strip().splitlines()
                    if lines:
                        import json as _json
                        last = _json.loads(lines[-1])
                        _n = last.get("n", 0)
                except Exception:
                    pass
                if _n == 0:
                    logger.error("[task:reply_training] done but n=0 — 训练无产出，检查数据源")
                    notifier.send_message(f"⚠️ 回复训练完成但 n=0（无有效训练数据）\n脚本 exit=0 但未产出题目，请检查 QCL 数据源或 KB 状态")
                else:
                    logger.info(f"[task:reply_training] done n={_n}")
                    notifier.send_message(f"✅ 夜间回复训练完成（{_n}题）")
            else:
                logger.error(f"[task:reply_training] failed: {result.stderr[:300]}")
                notifier.send_message(f"❌ 夜间回复训练失败\n{result.stderr[:300]}")
        except Exception as exc:
            logger.error(f"[task:reply_training] {exc}")
            try:
                notifier.send_message(f"❌ 夜间回复训练异常: {exc}")
            except Exception:
                pass

        if run_backfill:
            try:
                _fb_input = str(_BACKEND / "data" / "reply_trainer" / "feedback_log.jsonl")
                _diff_script = str(_BACKEND / "reply_diff_analyzer.py")
                subprocess.run(
                    [_PYTHON, _diff_script, "--backfill", "--input", _fb_input, "--limit", str(backfill_limit)],
                    timeout=3600, cwd=_PROJ_ROOT, env=_env,
                )
                logger.info("[task:reply_training] diff backfill done")
            except Exception as exc:
                logger.warning(f"[task:reply_training] diff backfill failed (non-critical): {exc}")
    finally:
        if _shutdown:
            _shutdown("reply_training")


def _task_run_script(_schedule_id: str = "", _schedule_command: list = None, **kwargs):
    """通用 script 调度 handler：执行 schedule.command，飞书通知结果。

    kwargs:
      preflight_local_llm=true      local LLM 启不来则跳过（旧行为，向后兼容）
      llm_strategy="preflight_skip"  同 preflight_local_llm=true
      llm_strategy="preflight_fallback"  local LLM 启不来则告警后继续（不跳过）
      llm_strategy="daytime_aware"  白天用在线LLM，通过 LLM_PROVIDER_OVERRIDE 注入子进程
    """
    cmd = [c.replace("__PYTHON__", _PYTHON) for c in (_schedule_command or [])]
    if not cmd:
        logger.error("[task:script:%s] command empty", _schedule_id)
        return
    notifier = _notifier()
    llm_strategy = kwargs.get("llm_strategy", "")
    env_extra: dict = {}

    _needs_shutdown = False

    if llm_strategy == "daytime_aware":
        try:
            sys.path.insert(0, str(_BACKEND))
            from services.local_llm_lifecycle import daytime_chain as _daytime_chain, ensure_running as _ensure_local
            chain = _daytime_chain(_schedule_id)
            if chain[0] != "local":
                env_extra["LLM_PROVIDER_OVERRIDE"] = chain[0]
                logger.info("[task:script:%s] daytime_aware → LLM_PROVIDER_OVERRIDE=%s", _schedule_id, chain[0])
            else:
                _needs_shutdown = _ensure_local()
        except Exception as exc:
            logger.warning("[task:script:%s] daytime_chain failed: %s", _schedule_id, exc)
    elif kwargs.get("preflight_local_llm") or llm_strategy in ("preflight_skip", "preflight_fallback"):
        try:
            sys.path.insert(0, str(_BACKEND))
            from services.local_llm_lifecycle import ensure_running as _ensure_local
            if _ensure_local():
                _needs_shutdown = True
            else:
                if llm_strategy == "preflight_fallback":
                    notifier.send_message(
                        f"⚠️ [{_schedule_id}] local LLM 三次自启失败，已降级到在线 LLM 继续运行"
                    )
                else:
                    notifier.send_message(
                        f"⚠️ [{_schedule_id}] local LLM 三次自启失败，任务将跳过（脚本依赖本地模型）"
                    )
                    return
        except Exception as exc:
            logger.warning("[task:script:%s] preflight_local_llm failed: %s", _schedule_id, exc)

    _run_env = {**os.environ, **env_extra} if env_extra else None
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=kwargs.get("timeout_seconds", 7200),
            cwd=_PROJ_ROOT,
            env=_run_env,
        )
        if result.returncode == 0:
            logger.info("[task:script:%s] succeeded", _schedule_id)
            notifier.send_message(f"✅ [{_schedule_id}] 脚本任务完成")
        else:
            logger.error("[task:script:%s] failed rc=%d stderr=%s", _schedule_id, result.returncode, result.stderr[:300])
            notifier.send_message(
                f"❌ [{_schedule_id}] 脚本任务失败 (rc={result.returncode})\n{result.stderr[:400]}"
            )
    except subprocess.TimeoutExpired:
        logger.error("[task:script:%s] timeout", _schedule_id)
        notifier.send_message(f"⏱️ [{_schedule_id}] 脚本任务超时")
    except Exception as exc:
        logger.error("[task:script:%s] exception: %s", _schedule_id, exc)
        try:
            notifier.send_message(f"🚨 [{_schedule_id}] 脚本任务异常: {exc}")
        except Exception:
            pass
    finally:
        if _needs_shutdown:
            try:
                from services.local_llm_lifecycle import shutdown_if_started_by_us as _shutdown
                _shutdown(_schedule_id)
            except Exception as _exc:
                logger.warning("[task:script:%s] shutdown failed: %s", _schedule_id, _exc)


def _task_jobmaster_agent_audit(**kwargs):
    """审计所有有 agent_hint 的 schedule，检查 agent_tasks 近期是否有成功执行记录。

    逻辑：
    - 扫描 data/schedules/*.json，取 enabled=True 且含 agent_hint 的条目
    - 按 cron 表达式估算执行周期
    - 若 2×周期内无 succeeded 行，则视为 overdue
    - overdue 列表非空时发飞书告警
    """
    import json as _json
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt, timedelta as _td

    sched_dir = _BACKEND / "data" / "schedules"
    db_path = _BACKEND / "data" / "sqlite" / "agent_tasks.db"

    if not db_path.exists():
        logger.warning("[task:agent_audit] agent_tasks.db not found at %s", db_path)
        return {"checked": 0, "overdue": []}

    overdue = []
    checked = 0
    now_utc = _dt.utcnow()

    try:
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
    except Exception as exc:
        logger.error("[task:agent_audit] db connect failed: %s", exc)
        return {"checked": 0, "overdue": []}

    with conn:
        for f in sorted(sched_dir.glob("*.json")):
            if f.name.startswith("deferred-") or f.name.startswith("__"):
                continue
            try:
                sched = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue

            if not sched.get("enabled", True):
                continue
            agent_hint = sched.get("agent_hint")
            if not agent_hint:
                continue

            cron = sched.get("cron", "")
            parts = cron.split()
            if len(parts) < 5:
                continue

            minute_p, hour_p, dom_p, _, dow_p = parts[:5]
            if dow_p != "*" and dom_p == "*":
                cadence_h = 7 * 24      # weekly
            elif dom_p != "*" and dow_p == "*":
                cadence_h = 30 * 24     # monthly
            elif hour_p == "*":
                cadence_h = 2           # sub-hourly / hourly
            else:
                cadence_h = 24          # daily

            max_age_h = cadence_h * 2
            since = (now_utc - _td(hours=max_age_h)).isoformat()
            sid = sched.get("id", f.stem)

            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM agent_tasks "
                    "WHERE schedule_id = ? AND status = 'succeeded' AND created_at >= ?",
                    (sid, since),
                ).fetchone()
                n_recent = row["n"] if row else 0
            except Exception as exc:
                logger.warning("[task:agent_audit] query failed for %s: %s", sid, exc)
                continue

            checked += 1
            if n_recent == 0:
                overdue.append({
                    "schedule_id": sid,
                    "agent": agent_hint,
                    "cadence_hours": cadence_h,
                    "max_age_hours": max_age_h,
                    "msg": f"{sid} ({agent_hint}) {int(max_age_h)}h 内无成功执行记录",
                })

    logger.info("[task:agent_audit] checked=%d overdue=%d", checked, len(overdue))

    if overdue:
        try:
            notifier = _notifier()
            lines = "\n".join(f"  - {o['msg']}" for o in overdue[:10])
            suffix = f"\n  ... 共 {len(overdue)} 个" if len(overdue) > 10 else ""
            notifier.send_message(
                f"⚠️ Agent 自监督告警：{len(overdue)} 个 schedule 过期未跑\n{lines}{suffix}"
            )
        except Exception as exc:
            logger.warning("[task:agent_audit] feishu notify failed: %s", exc)

    return {"checked": checked, "overdue_count": len(overdue), "overdue": overdue}


def _task_jobmaster_backfill_sparse(**kwargs):
    """JobMaster backfill_sparse 模式：扫描 0/1 次运行的 schedule，自主决策补跑。"""
    _run_jobmaster("backfill_sparse")


def _run_jobmaster(mode: str):
    try:
        result = subprocess.run(
            [_PYTHON, _JM_SCRIPT, "--mode", mode],
            capture_output=True, text=True, timeout=300, cwd=_JM_CWD,
        )
        if result.returncode != 0:
            logger.error(f"[task:jobmaster_{mode}] failed: {result.stderr[:300]}")
            try:
                _notifier().send_message(f"❌ JobMaster[{mode}] 失败\n{result.stderr[:400]}")
            except Exception:
                pass
        else:
            logger.info(f"[task:jobmaster_{mode}] done")
    except Exception as exc:
        logger.error(f"[task:jobmaster_{mode}] exception: {exc}")


def _task_jobmaster_daily(**kwargs):
    _run_jobmaster("daily")


def _check_training_health():
    """
    训练回路健康自愈 — 感知→诊断→修复，不依赖人工指令。
    问题等级：
      P1（自动修复）: lessons=0 且有历史成功训练 → 补跑一次训练
      P1（自动修复）: 连续 2 次 n=0 → 补跑训练
      P2（告警+建议）: 路径/读取异常 → 飞书说明原因
    """
    import json as _json
    notifier = _notifier()
    _train_dir = _BACKEND.parent.parent / "conclusion" / "_local" / "training"
    fixes_applied = []
    alerts = []

    # ── 1. trainer_state 检查 ─────────────────────────────────────────────
    state_path = _train_dir / "trainer_state.json"
    lessons_count = 0
    total_questions = 0
    if not state_path.exists():
        alerts.append("trainer_state.json 不存在（训练从未成功运行）")
    else:
        try:
            state = _json.loads(state_path.read_text(encoding="utf-8"))
            lessons = state.get("b_cumulative_lessons", [])
            lessons_count = len(lessons)
            total_questions = state.get("total_questions", 0)
            if lessons_count == 0 and total_questions > 0:
                # 训练跑过但教训为空 → 自动补跑一次（20题快速恢复）
                logger.warning("[training_health] b_cumulative_lessons=0 但 total_questions>0，自动补跑训练")
                try:
                    _task_reply_training(questions=20, stop_hour=23, run_backfill=False)
                    fixes_applied.append(f"自动补跑训练 20 题（lessons=0 恢复）")
                except Exception as exc:
                    alerts.append(f"自动补跑失败: {exc}")
        except Exception as e:
            alerts.append(f"trainer_state 读取失败: {e}")

    # ── 2. 连续 n=0 检查 ─────────────────────────────────────────────────
    metrics_path = _train_dir / "training_metrics.jsonl"
    if metrics_path.exists():
        try:
            lines = [l for l in metrics_path.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
            recent = [_json.loads(l) for l in lines[-3:]]
            zero_runs = [r for r in recent if r.get("n", 1) == 0]
            if len(zero_runs) >= 2 and lessons_count > 0:
                # lessons 还在但连续 n=0 → 数据源可能有问题，补跑一次探测
                logger.warning(f"[training_health] 连续 {len(zero_runs)} 次 n=0，自动探测数据源")
                try:
                    _task_reply_training(questions=10, stop_hour=23, run_backfill=False)
                    fixes_applied.append(f"连续 n=0，自动探测训练 10 题")
                except Exception as exc:
                    alerts.append(f"探测训练失败: {exc}")
        except Exception as e:
            alerts.append(f"training_metrics 读取失败: {e}")

    # ── 报告 ─────────────────────────────────────────────────────────────
    if fixes_applied:
        msg = "🔧 [JobMaster] 训练回路自愈完成：\n" + "\n".join(f"✓ {f}" for f in fixes_applied)
        if alerts:
            msg += "\n⚠️ 同时发现需人工关注：\n" + "\n".join(f"- {a}" for a in alerts)
        notifier.send_message(msg)
        logger.info(f"[training_health] 自愈: {'; '.join(fixes_applied)}")
    elif alerts:
        msg = "⚠️ [JobMaster] 训练回路异常（自动修复无效，需人工介入）：\n" + "\n".join(f"- {a}" for a in alerts)
        notifier.send_message(msg)
        logger.warning(f"[training_health] 无法自愈: {'; '.join(alerts)}")


def _task_jobmaster_monitor(**kwargs):
    _run_jobmaster("monitor")
    _check_training_health()


def _task_jobmaster_heartbeat(**kwargs):
    _run_jobmaster("heartbeat")


def _task_daily_summary(**kwargs):
    try:
        from agents.daily_summary_agent import DailySummaryAgent
        result = DailySummaryAgent().run_task(payload=kwargs, trigger_src="schedule:daily")
        logger.info(f"[task:daily_summary] done: {result}")
    except Exception as exc:
        logger.exception(f"[task:daily_summary] failed: {exc}")
        try:
            from services.feishu_notifier import get_notifier as _gn
            from datetime import date as _d
            _gn().send_message(f"❌ 日报生成失败 {_d.today()}\n{exc}\n请在 agents.html 检查")
        except Exception:
            pass


def _task_daily_summary_watchdog(**kwargs):
    from datetime import date as _d, timedelta as _td
    from pathlib import Path as _P
    yesterday = _d.today() - _td(days=1)
    archive = _P(f"conclusion/daily_reports/{yesterday}.md")
    if not archive.exists():
        logger.warning("[task:daily_summary_watchdog] archive missing, re-running daily_summary")
        _task_daily_summary(date=str(yesterday))
        return
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P2
        db = _P2("data/sqlite/agent_tasks.db")
        if db.exists():
            conn = _sq.connect(str(db))
            rows = conn.execute(
                "SELECT id FROM agent_tasks WHERE agent_name='daily_summary'"
                " AND json_extract(payload_json,'$.kind')='daily_report_failed'"
                " AND created_at >= date('now','-1 day') AND status != 'succeeded'"
            ).fetchall()
            conn.close()
            if rows:
                md = archive.read_text(encoding="utf-8")
                from services.feishu_notifier import get_notifier as _gn
                if _gn().send_message(md):
                    conn2 = _sq.connect(str(db))
                    for r in rows:
                        conn2.execute("UPDATE agent_tasks SET status='succeeded' WHERE id=?", (r[0],))
                    conn2.commit()
                    conn2.close()
                    logger.info("[task:daily_summary_watchdog] 补发成功")
    except Exception as exc:
        logger.error(f"[task:daily_summary_watchdog] {exc}")


def _task_vacation_schedule_cleanup(purpose: str = "vacation_window_2026_05", **kwargs):
    import json as _json
    import glob as _glob
    disabled = []
    for p in _glob.glob("data/schedules/*.json"):
        try:
            with open(p) as f:
                s = _json.load(f)
            if s.get("purpose") == purpose and s.get("enabled", False):
                s["enabled"] = False
                with open(p, "w") as f:
                    _json.dump(s, f, ensure_ascii=False, indent=2)
                disabled.append(s["id"])
        except Exception:
            pass
    logger.info(f"[task:vacation_cleanup] disabled {len(disabled)} schedules: {disabled}")
    try:
        from services.feishu_notifier import get_notifier as _gn
        _gn().send_message(f"✅ 假期调度已清理，共禁用 {len(disabled)} 条：{', '.join(disabled)}")
    except Exception:
        pass


# ─── Automation Poll Loop ─────────────────────────────────────────────────────

_board_service_instance = None
_board_service_lock = threading.Lock()


def _get_board_service():
    global _board_service_instance
    with _board_service_lock:
        if _board_service_instance is None:
            from board_service_chroma import BoardService as ChromaBoardService
            from llm_service import LLMService
            _board_service_instance = ChromaBoardService(LLMService(), api_key=None, allow_download=False)
        return _board_service_instance


def _automation_poll_loop():
    INTERVAL = 600  # 10 分钟
    time.sleep(30)
    while True:
        try:
            result = _get_board_service().run_all_enabled_rules()
            if result.get("ran", 0) > 0:
                logger.info(f"[AutomationPoll] {result['ran']}规则, 执行{result.get('total_executed',0)}条")
        except Exception as e:
            logger.error(f"[AutomationPoll] 异常: {e}")
        time.sleep(INTERVAL)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    logger.info("jobmaster daemon 启动中...")

    # A3: KB 写操作队列（daemon 端消费）
    try:
        from services.kb_write_dispatcher import register_kb_handlers
        register_kb_handlers()
        logger.info("[KB] write dispatcher 已启动")
    except Exception as e:
        logger.error(f"[KB] write dispatcher 启动失败: {e}")

    # Task handlers 注册
    from services.scheduler_service import start_scheduler, register_task_handler
    register_task_handler("weekly_report", _task_weekly_report)
    register_task_handler("monthly_report", _task_monthly_report)
    register_task_handler("nightly_exploration", _task_nightly_exploration)
    register_task_handler("reply_training", _task_reply_training)
    register_task_handler("jobmaster_daily", _task_jobmaster_daily)
    register_task_handler("jobmaster_monitor", _task_jobmaster_monitor)
    register_task_handler("jobmaster_heartbeat", _task_jobmaster_heartbeat)
    register_task_handler("daily_summary", _task_daily_summary)
    register_task_handler("daily_summary_watchdog", _task_daily_summary_watchdog)
    register_task_handler("vacation_schedule_cleanup", _task_vacation_schedule_cleanup)
    register_task_handler("script", _task_run_script)

    def _task_agent_trigger(**kwargs):
        """noop placeholder：实际 agent 触发已由 main 后端处理，daemon 无需执行。"""
        logger.debug("[task:agent_trigger] noop — handled by main backend")

    register_task_handler("agent_trigger", _task_agent_trigger)
    register_task_handler("jobmaster_agent_audit", _task_jobmaster_agent_audit)
    register_task_handler("jobmaster_backfill_sparse", _task_jobmaster_backfill_sparse)

    # Scheduler cron
    try:
        start_scheduler()
        logger.info("[Scheduler] cron 已启动")
    except Exception as e:
        logger.error(f"[Scheduler] 启动失败: {e}")

    # PM scheduler
    try:
        from services.pm_scheduler import start_pm_scheduler
        start_pm_scheduler(sync_interval=5, process_interval=10, overdue_interval=60)
        logger.info("[PM] 调度器已启动")
    except Exception as e:
        logger.error(f"[PM] 调度器启动失败: {e}")

    # Automation poll thread
    threading.Thread(target=_automation_poll_loop, daemon=True, name="automation-poll").start()
    logger.info("[Automation] 自动化规则轮询已启动")

    # 启动期孤儿任务恢复（防永久 stuck，参考 multica RecoverOrphanedTasks）
    _recover_orphaned_tasks()

    # 加载 multica 借鉴守则（playbook.md 存在时记录通路已就绪）
    _pb = _load_playbook()
    if _pb:
        logger.info(f"[JobMaster] playbook loaded: {len(_pb)} chars")

    logger.info("jobmaster daemon 就绪")

    # 主线程保活；SIGTERM/SIGINT 退出
    def _bye(*_):
        logger.info("jobmaster daemon 收到停止信号，退出")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    while True:
        time.sleep(30)


if __name__ == "__main__":
    main()
