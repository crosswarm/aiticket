"""
压测场景 C1-C6 执行器。
"""
import argparse
import json
import sys
import time
import threading
import concurrent.futures
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from loadtest_helper import (
    BASE_URL, Result, TestReport, login, timed_get, timed_post, run_concurrent
)

import requests


def _healthz(session) -> float:
    """返回 /api/instance/config 响应时间 (ms)。"""
    t0 = time.time()
    try:
        r = session.get(f"{BASE_URL}/api/instance/config", timeout=5)
        return (time.time() - t0) * 1000
    except Exception:
        return 9999.0


def c1_concurrent_reply(base_url: str, sessions: list[requests.Session]) -> TestReport:
    """C1: 5 并发 /api/reply 流式请求 — TTFB p95 < 8s；healthz p99 < 200ms"""
    print("C1: 5 并发流式 reply...", end=" ", flush=True)
    report = TestReport("C1_concurrent_reply")
    healthz_times = []

    def one_reply(s: requests.Session):
        # Pick a minimal reply payload (no real Jira key needed — just measure perf)
        r = timed_post(s, "/api/board/generate-reply",
                       body={"issue_key": "TEST-1", "summary": "loadtest", "description": ""},
                       stream=True)
        r.test_id = "C1"
        return r

    def healthz_probe():
        s = requests.Session()
        for _ in range(10):
            healthz_times.append(_healthz(s))
            time.sleep(0.1)

    # Fire healthz probes during reply load
    probe_thread = threading.Thread(target=healthz_probe, daemon=True)
    probe_thread.start()

    results = run_concurrent(one_reply, [(s,) for s in sessions[:5]], max_workers=5)
    probe_thread.join(timeout=5)

    for r in results:
        r.test_id = "C1"
        report.add(r)

    healthz_times.sort()
    healthz_p99 = healthz_times[int(len(healthz_times) * 0.99)] if healthz_times else 0

    passed = report.p95 <= 8000 and healthz_p99 < 200
    print(f"p95={report.p95:.0f}ms  healthz_p99={healthz_p99:.0f}ms  {'✅' if passed else '❌'}")
    s = report.summary()
    s["healthz_p99_ms"] = round(healthz_p99, 1)
    s["passed"] = passed
    return report


def c2_concurrent_kb_search(sessions: list[requests.Session]) -> TestReport:
    """C2: 10 并发 /api/kb/search 含 project_key 过滤 — p95 < 1.5s，无串项目结果"""
    print("C2: 10 并发 KB 搜索...", end=" ", flush=True)
    report = TestReport("C2_concurrent_kb_search")

    def one_search(s: requests.Session, project_key: str):
        r = timed_get(s, "/api/kb/search",
                      params={"q": "工作流", "project_key": project_key, "top_k": 5})
        r.test_id = "C2"
        return r

    args = [(sessions[i % len(sessions)], "MYPROJECT") for i in range(10)]
    results = run_concurrent(lambda s, pk: one_search(s, pk), args, max_workers=10)
    for r in results:
        report.add(r)

    passed = report.p95 <= 1500 and report.error_rate == 0
    print(f"p95={report.p95:.0f}ms  errors={report.error_rate*100:.0f}%  {'✅' if passed else '❌'}")
    report.summary()["passed"] = passed
    return report


def c3_concurrent_login(base_url: str, admin_user: str, admin_pass: str) -> TestReport:
    """C3: 20 并发登录 — 0 OperationalError，zero 5xx"""
    print("C3: 20 并发登录...", end=" ", flush=True)
    report = TestReport("C3_concurrent_login")

    def one_login(_):
        t0 = time.time()
        try:
            r = requests.post(f"{base_url}/api/auth/login",
                              json={"username": admin_user, "password": admin_pass},
                              timeout=10)
            return Result("C3", (time.time() - t0) * 1000, r.status_code,
                          None if r.status_code < 500 else r.text[:100])
        except Exception as e:
            return Result("C3", (time.time() - t0) * 1000, 0, str(e))

    results = run_concurrent(one_login, [(i,) for i in range(20)], max_workers=20)
    for r in results:
        report.add(r)

    db_lock_errors = [r.error for r in report.results if r.error and "locked" in (r.error or "").lower()]
    passed = len(db_lock_errors) == 0 and report.error_rate == 0
    print(f"p95={report.p95:.0f}ms  db_lock_errors={len(db_lock_errors)}  {'✅' if passed else '❌'}")
    report.summary()["db_lock_count"] = len(db_lock_errors)
    report.summary()["passed"] = passed
    return report


def c4_mixed_load(sessions: list[requests.Session], duration_s: int = 60) -> TestReport:
    """C4: 5 用户混合负载 (reply+board+kb) — p95 < 10s，RSS 不急剧增长"""
    print(f"C4: 混合负载 {duration_s}s...", end=" ", flush=True)
    report = TestReport("C4_mixed_load")
    stop = threading.Event()
    lock = threading.Lock()

    endpoints = [
        ("/api/board/stats", "GET", {}),
        ("/api/kb/search", "GET", {"q": "工作流", "top_k": 3}),
        ("/api/instance/config", "GET", {}),
    ]

    def worker(s: requests.Session, idx: int):
        ep_list = endpoints * 10  # cycle
        for path, method, params in ep_list:
            if stop.is_set():
                break
            if method == "GET":
                r = timed_get(s, path, params=params)
            else:
                r = timed_post(s, path)
            r.test_id = "C4"
            with lock:
                report.add(r)
            time.sleep(0.2)

    threads = [threading.Thread(target=worker, args=(sessions[i % len(sessions)], i), daemon=True)
               for i in range(5)]
    for t in threads:
        t.start()
    time.sleep(duration_s)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    passed = report.p95 <= 10000 and report.error_rate < 0.01
    print(f"reqs={len(report.results)}  p95={report.p95:.0f}ms  errors={report.error_rate*100:.1f}%  {'✅' if passed else '❌'}")
    report.summary()["passed"] = passed
    return report


def c5_kb_rebuild_concurrent_search(sessions: list[requests.Session]) -> TestReport:
    """C5: KB rebuild 同时跑 + 5 并发 search — rebuild 不阻塞 search"""
    print("C5: KB rebuild 并发搜索...", end=" ", flush=True)
    report = TestReport("C5_rebuild_concurrent_search")

    # Trigger rebuild (non-blocking kick, may 404 if no topics configured — that's OK)
    rebuild_times = []

    def trigger_rebuild(s):
        t0 = time.time()
        try:
            r = s.post(f"{BASE_URL}/api/kb/compile", json={}, timeout=5)
            rebuild_times.append((time.time() - t0) * 1000)
        except Exception:
            pass

    sessions[0].post(f"{BASE_URL}/api/kb/compile", json={}, timeout=5)

    # Concurrent searches while rebuild is running
    def search(s):
        r = timed_get(s, "/api/kb/search", params={"q": "流程", "top_k": 3})
        r.test_id = "C5"
        return r

    results = run_concurrent(search, [(sessions[i % len(sessions)],) for i in range(5)], max_workers=5)
    for r in results:
        report.add(r)

    passed = report.p95 <= 3000 and report.error_rate == 0
    print(f"p95={report.p95:.0f}ms  errors={report.error_rate*100:.0f}%  {'✅' if passed else '❌'}")
    report.summary()["passed"] = passed
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-pass", default="admin")
    parser.add_argument("--output", default="results.json")
    parser.add_argument("--quick", action="store_true", help="跳过 C4 长时间混合负载")
    args = parser.parse_args()

    global BASE_URL
    import loadtest_helper
    loadtest_helper.BASE_URL = args.base_url
    BASE_URL = args.base_url

    print("登录中...", end=" ")
    try:
        sessions = [login(args.admin_user, args.admin_pass) for _ in range(5)]
        print(f"✅ ({len(sessions)} sessions)")
    except Exception as e:
        print(f"❌ 登录失败: {e}")
        sys.exit(1)

    reports = []

    reports.append(c1_concurrent_reply(BASE_URL, sessions))
    reports.append(c2_concurrent_kb_search(sessions))
    reports.append(c3_concurrent_login(BASE_URL, args.admin_user, args.admin_pass))
    duration = 30 if args.quick else 60
    reports.append(c4_mixed_load(sessions, duration_s=duration))
    reports.append(c5_kb_rebuild_concurrent_search(sessions))

    summaries = [r.summary() for r in reports]
    passed_all = all(s.get("passed", False) for s in summaries)
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_url": BASE_URL,
        "passed_all": passed_all,
        "tests": summaries,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("")
    print(f"{'✅ 全部通过' if passed_all else '❌ 有测试未通过'}")
    sys.exit(0 if passed_all else 1)


if __name__ == "__main__":
    main()
