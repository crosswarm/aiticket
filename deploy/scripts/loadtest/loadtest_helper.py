"""
压测辅助库 — 登录、请求发送、结果收集。
"""
import concurrent.futures
import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

BASE_URL = "http://127.0.0.1:18000"


@dataclass
class Result:
    test_id: str
    duration_ms: float
    status_code: int
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300


@dataclass
class TestReport:
    test_id: str
    results: List[Result] = field(default_factory=list)

    def add(self, r: Result):
        self.results.append(r)

    @property
    def p50(self) -> float:
        times = sorted(r.duration_ms for r in self.results)
        if not times:
            return 0
        return times[len(times) // 2]

    @property
    def p95(self) -> float:
        times = sorted(r.duration_ms for r in self.results)
        if not times:
            return 0
        return times[int(len(times) * 0.95)]

    @property
    def error_rate(self) -> float:
        if not self.results:
            return 0
        return sum(1 for r in self.results if not r.ok) / len(self.results)

    def passed(self, max_p95_ms: float = 8000, max_error_rate: float = 0.0) -> bool:
        return self.p95 <= max_p95_ms and self.error_rate <= max_error_rate

    def summary(self) -> dict:
        return {
            "test_id": self.test_id,
            "count": len(self.results),
            "p50_ms": round(self.p50, 1),
            "p95_ms": round(self.p95, 1),
            "error_rate": round(self.error_rate * 100, 1),
            "errors": [r.error for r in self.results if r.error],
        }


def login(username: str, password: str) -> requests.Session:
    """登录并返回携带 cookie 的 Session。"""
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
                json={"username": username, "password": password},
                timeout=10)
    r.raise_for_status()
    return s


def timed_get(session: requests.Session, path: str, params: dict = None) -> Result:
    t0 = time.time()
    try:
        r = session.get(f"{BASE_URL}{path}", params=params, timeout=30)
        return Result("", (time.time() - t0) * 1000, r.status_code)
    except Exception as e:
        return Result("", (time.time() - t0) * 1000, 0, str(e))


def timed_post(session: requests.Session, path: str, body: dict = None, stream: bool = False) -> Result:
    t0 = time.time()
    try:
        r = session.post(f"{BASE_URL}{path}", json=body, timeout=60, stream=stream)
        if stream:
            # consume stream to measure TTFB only
            first_chunk = True
            for _ in r.iter_content(chunk_size=512):
                if first_chunk:
                    ttfb = (time.time() - t0) * 1000
                    first_chunk = False
                    break
            r.close()
            return Result("", ttfb, r.status_code)
        return Result("", (time.time() - t0) * 1000, r.status_code)
    except Exception as e:
        return Result("", (time.time() - t0) * 1000, 0, str(e))


def run_concurrent(fn, args_list: list, max_workers: int) -> List[Result]:
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fn, *args): args for args in args_list}
        for fut in concurrent.futures.as_completed(futs):
            try:
                r = fut.result()
                results.append(r)
            except Exception as e:
                results.append(Result("", 0, 0, str(e)))
    return results
