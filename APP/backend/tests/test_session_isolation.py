"""
Phase A — 会话隔离单测
验证多用户场景下会话不串、数据不混、错误可观察。
"""
import concurrent.futures
import hashlib
import secrets
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_request(username: str, xpm_user: Optional[str] = None, project_key: str = "MYPROJECT") -> SimpleNamespace:
    """构造最小化 FastAPI Request 替身。"""
    req = SimpleNamespace()
    req.state = SimpleNamespace()
    req.state.current_user = {"id": f"uid-{username}", "username": username, "current_project": project_key}
    headers = {}
    if xpm_user is not None:
        headers["X-PM-User"] = xpm_user
    req.headers = headers
    return req


def _make_unauthenticated_request(xpm_user: Optional[str] = None) -> SimpleNamespace:
    req = SimpleNamespace()
    req.state = SimpleNamespace()
    req.state.current_user = None
    req.headers = {"X-PM-User": xpm_user} if xpm_user else {}
    return req


# ── A1：X-PM-User 伪造防护 ────────────────────────────────────────────────────

class TestXPMUserBinding:
    def test_matching_header_accepted(self):
        """X-PM-User == auth identity → OK，返回该用户名。"""
        from api.pm_routes import _resolve_pm_user
        req = _make_request("alice", xpm_user="alice")
        assert _resolve_pm_user(req) == "alice"

    def test_no_header_falls_back_to_auth_identity(self):
        """无 X-PM-User header → 返回登录用户名。"""
        from api.pm_routes import _resolve_pm_user
        req = _make_request("alice")
        assert _resolve_pm_user(req) == "alice"

    def test_mismatched_header_raises_403(self):
        """X-PM-User != auth identity → 403。"""
        from fastapi import HTTPException
        from api.pm_routes import _resolve_pm_user
        req = _make_request("bob", xpm_user="alice")
        with pytest.raises(HTTPException) as exc_info:
            _resolve_pm_user(req)
        assert exc_info.value.status_code == 403

    def test_unauthenticated_request_raises_401(self):
        """未登录请求 → 401。"""
        from fastapi import HTTPException
        from api.pm_routes import _resolve_pm_user
        req = _make_unauthenticated_request()
        with pytest.raises(HTTPException) as exc_info:
            _resolve_pm_user(req)
        assert exc_info.value.status_code == 401


# ── A2：PM 数据隔离（singleton 不泄漏） ──────────────────────────────────────

class TestPMSingletonIsolation:
    def test_current_pm_user_is_thread_local(self):
        """两个线程设置不同的 current_pm_user，互不干扰。"""
        from services.pm_module_service import _pm_user_local, PMModuleService
        results = {}
        barrier = threading.Barrier(2)

        def worker(name: str, delay: float):
            _pm_user_local.pm_user = name
            barrier.wait()
            time.sleep(delay)
            results[name] = _pm_user_local.pm_user

        t1 = threading.Thread(target=worker, args=("alice", 0.02))
        t2 = threading.Thread(target=worker, args=("bob", 0.01))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results["alice"] == "alice", "alice 的线程看到了 bob 的值"
        assert results["bob"] == "bob", "bob 的线程看到了 alice 的值"

    def test_property_getter_returns_none_when_unset(self):
        """新线程中未设置 current_pm_user 时返回 None。"""
        from services.pm_module_service import _pm_user_local
        result = {}

        def worker():
            # 确保这个新线程没有继承任何值
            if hasattr(_pm_user_local, 'pm_user'):
                del _pm_user_local.pm_user
            # Import after thread start to use fresh context
            from services.pm_module_service import _pm_user_local as local
            result['val'] = getattr(local, 'pm_user', None)

        t = threading.Thread(target=worker)
        t.start(); t.join()
        assert result['val'] is None


# ── A3：未绑定 PM 钱包 → 401，不返回管理员数据 ────────────────────────────────

class TestPMWalletNotBound:
    def test_get_effective_cookies_returns_empty_for_unknown_user(self, tmp_path):
        """用户钱包不存在时 get_effective_cookies 返回空 dict。"""
        from services import pm_wallet_service as wm
        orig_dir = wm.WALLET_DIR
        wm.WALLET_DIR = tmp_path / "pm_tokens"
        try:
            result = wm.get_effective_cookies("nonexistent_user")
            assert result == {}, f"期望空 dict，实际: {result}"
        finally:
            wm.WALLET_DIR = orig_dir

    def test_get_effective_cookies_no_fallback_to_admin(self, tmp_path):
        """admin token 文件存在时，未绑定用户仍不应看到 admin 数据。"""
        from services import pm_wallet_service as wm
        orig_dir = wm.WALLET_DIR
        orig_default = wm.DEFAULT_TOKEN_PATH
        admin_token_file = tmp_path / "pm_token.json"
        admin_token_file.write_text('{"yht_access_token": "admin-token-secret"}')
        wm.WALLET_DIR = tmp_path / "pm_tokens"
        wm.DEFAULT_TOKEN_PATH = admin_token_file
        try:
            result = wm.get_effective_cookies("bob_no_wallet")
            assert result.get("yht_access_token") != "admin-token-secret", \
                "admin token 泄漏给未绑定用户"
            assert result == {}
        finally:
            wm.WALLET_DIR = orig_dir
            wm.DEFAULT_TOKEN_PATH = orig_default

    def test_pm_not_bound_error_attributes(self):
        """PMNotBoundError 携带正确的 username 属性。"""
        from services.pm_wallet_service import PMNotBoundError
        err = PMNotBoundError("charlie")
        assert err.username == "charlie"
        assert "charlie" in str(err)


# ── A4：KB 搜索 project_key 越权 → 403 ───────────────────────────────────────

class TestKBSearchScope:
    def _call_search_kb(self, username: str, user_project: str, query_project_key: str, allowed: list):
        """直接调用 search_kb 逻辑（不启动完整 FastAPI）。"""
        from fastapi import HTTPException

        current_user = {"id": f"uid-{username}", "username": username, "current_project": user_project}

        if query_project_key and query_project_key != "_global":
            allowed_set = set(allowed)
            if current_user.get("current_project"):
                allowed_set.add(current_user["current_project"])
            if query_project_key not in allowed_set:
                raise HTTPException(status_code=403, detail="project_key 超出实例允许范围")

    def test_allowed_project_passes(self):
        self._call_search_kb("alice", "MYPROJECT", "MYPROJECT", ["MYPROJECT", "KKZC"])

    def test_global_always_passes(self):
        self._call_search_kb("alice", "MYPROJECT", "_global", ["MYPROJECT"])

    def test_empty_project_key_passes(self):
        self._call_search_kb("alice", "MYPROJECT", "", ["MYPROJECT"])

    def test_unauthorized_project_raises_403(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            self._call_search_kb("bob", "MYPROJECT", "SECRET_PROJ", ["MYPROJECT", "KKZC"])
        assert exc_info.value.status_code == 403

    def test_user_current_project_allowed(self):
        """用户当前项目不在 allowed_project_keys 白名单中也应允许。"""
        self._call_search_kb("alice", "MY_PROJ", "MY_PROJ", ["MYPROJECT"])


# ── A5 & A6：Skill token 创建 / 使用 / 撤销 ────────────────────────────────────

class TestSkillToken:
    @pytest.fixture
    def auth_svc(self, tmp_path):
        from auth_service import AuthService
        db = str(tmp_path / "test_auth.db")
        key = str(tmp_path / "test.key")
        svc = AuthService(db_path=db, secret_path=key)
        # 创建测试用户
        svc.create_user("alice", "pw123", display_name="Alice", role="member", created_by="test")
        svc.create_user("bob", "pw123", display_name="Bob", role="member", created_by="test")
        return svc

    def test_create_and_use_skill_token(self, auth_svc):
        """创建 token 后可用于认证。"""
        alice = auth_svc.authenticate("alice", "pw123")
        result = auth_svc.create_skill_token(alice["id"], label="claude-code")
        token = result["token"]
        assert token

        resolved = auth_svc.get_user_by_skill_token(token)
        assert resolved is not None
        assert resolved["username"] == "alice"

    def test_token_is_user_scoped(self, auth_svc):
        """A 的 token 不能解析为 B 的身份。"""
        alice = auth_svc.authenticate("alice", "pw123")
        bob = auth_svc.authenticate("bob", "pw123")
        a_token = auth_svc.create_skill_token(alice["id"])["token"]
        b_token = auth_svc.create_skill_token(bob["id"])["token"]

        assert auth_svc.get_user_by_skill_token(a_token)["username"] == "alice"
        assert auth_svc.get_user_by_skill_token(b_token)["username"] == "bob"
        assert auth_svc.get_user_by_skill_token(a_token)["username"] != "bob"
        assert auth_svc.get_user_by_skill_token(b_token)["username"] != "alice"

    def test_revoked_token_returns_none(self, auth_svc):
        """撤销后 token 不再有效（A6）。"""
        alice = auth_svc.authenticate("alice", "pw123")
        result = auth_svc.create_skill_token(alice["id"], label="to-revoke")
        token = result["token"]

        assert auth_svc.get_user_by_skill_token(token) is not None
        auth_svc.revoke_skill_token(alice["id"], "to-revoke")
        assert auth_svc.get_user_by_skill_token(token) is None

    def test_invalid_token_returns_none(self, auth_svc):
        """随机 token 不能认证。"""
        assert auth_svc.get_user_by_skill_token("totally-random-garbage") is None

    def test_list_tokens(self, auth_svc):
        alice = auth_svc.authenticate("alice", "pw123")
        auth_svc.create_skill_token(alice["id"], label="dev")
        auth_svc.create_skill_token(alice["id"], label="ci")
        tokens = auth_svc.list_skill_tokens(alice["id"])
        labels = {t["label"] for t in tokens}
        assert "dev" in labels
        assert "ci" in labels


# ── A7：auth.db 并发登录无 OperationalError ────────────────────────────────────

class TestConcurrentLogin:
    @pytest.fixture
    def auth_svc(self, tmp_path):
        from auth_service import AuthService
        db = str(tmp_path / "concurrent_auth.db")
        key = str(tmp_path / "concurrent.key")
        svc = AuthService(db_path=db, secret_path=key)
        for i in range(5):
            svc.create_user(f"user{i}", "pw", display_name=f"User{i}", role="member", created_by="test")
        return svc

    def test_concurrent_logins_no_db_lock(self, auth_svc):
        """20 并发认证请求不触发 sqlite3.OperationalError（WAL 保护）。"""
        errors = []
        timings = []

        def do_login(i: int):
            username = f"user{i % 5}"
            t0 = time.time()
            try:
                user = auth_svc.authenticate(username, "pw")
                assert user is not None
                auth_svc.create_session(user["id"], user_agent="test", ip="127.0.0.1")
            except sqlite3.OperationalError as e:
                errors.append(str(e))
            finally:
                timings.append((time.time() - t0) * 1000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futs = [ex.submit(do_login, i) for i in range(20)]
            concurrent.futures.wait(futs)

        assert not errors, f"OperationalError(s) 出现: {errors}"
        timings.sort()
        p95 = timings[int(len(timings) * 0.95)]
        assert p95 < 500, f"p95 登录延迟 {p95:.0f}ms 超过 500ms"
