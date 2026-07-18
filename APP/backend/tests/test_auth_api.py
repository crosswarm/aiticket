import importlib
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_HEADERS = {"X-AiTicket-CSRF": "1"}


def load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("APP_AUTH_SECRET_PATH", str(tmp_path / "auth.key"))

    if "main" in sys.modules:
        del sys.modules["main"]

    import main

    return importlib.reload(main)


def test_bootstrap_login_and_admin_role_enforcement(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    client = TestClient(main.app)

    bootstrap_status = client.get("/api/auth/bootstrap-status")
    assert bootstrap_status.status_code == 200
    assert bootstrap_status.json()["bootstrap_required"] is True

    bootstrap_response = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "secret-pass",
            "display_name": "管理员",
        },
    )
    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["user"]["role"] == "admin"

    login_response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret-pass"},
    )
    assert login_response.status_code == 200
    assert login_response.json()["user"]["username"] == "admin"

    me_response = client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["user"]["role"] == "admin"

    create_user_response = client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "alice",
            "password": "member-pass",
            "display_name": "Alice",
            "role": "member",
        },
    )
    assert create_user_response.status_code == 200
    assert create_user_response.json()["user"]["role"] == "member"

    client.post("/api/auth/logout")
    member_login_response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "member-pass"},
    )
    assert member_login_response.status_code == 200

    forbidden_response = client.get("/api/admin/users")
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["detail"] == "Admin access required"


def test_member_can_save_and_read_masked_jira_binding(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    client = TestClient(main.app)

    client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "secret-pass",
            "display_name": "管理员",
        },
    )
    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret-pass"},
    )
    client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "alice",
            "password": "member-pass",
            "display_name": "Alice",
            "role": "member",
        },
    )
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "member-pass"},
    )

    save_response = client.put(
        "/api/settings/jira-binding",
        json={
            "jira_username": "alice.jira",
            "jira_api_token": "jira-secret-token",
            "jira_base_url": "https://jira.example.com",
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["binding"]["has_token"] is True

    get_response = client.get("/api/settings/jira-binding")
    assert get_response.status_code == 200
    assert get_response.json()["binding"]["jira_username"] == "alice.jira"
    assert "jira-secret-token" not in get_response.text


def test_admin_user_management_and_member_password_change(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    client = TestClient(main.app)

    bootstrap = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "secret-pass", "display_name": "管理员"},
    )
    admin_id = bootstrap.json()["user"]["id"]
    assert client.get("/settings.html").status_code == 200

    created = client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={"username": "alice", "display_name": "Alice", "role": "member"},
    )
    assert created.status_code == 200
    created_data = created.json()
    member_id = created_data["user"]["id"]
    initial_password = created_data["temporary_password"]
    assert len(initial_password) >= 20
    assert "password_hash" not in created.text

    duplicate = client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={"username": "alice", "display_name": "Another Alice", "role": "member"},
    )
    assert duplicate.status_code == 409

    updated = client.patch(
        f"/api/admin/users/{member_id}",
        headers=CSRF_HEADERS,
        json={"display_name": "爱丽丝", "is_active": True},
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["display_name"] == "爱丽丝"

    promoted = client.patch(
        f"/api/admin/users/{member_id}",
        headers=CSRF_HEADERS,
        json={"role": "admin"},
    )
    assert promoted.json()["user"]["role"] == "admin"
    restored_member = client.patch(
        f"/api/admin/users/{member_id}",
        headers=CSRF_HEADERS,
        json={"role": "member"},
    )
    assert restored_member.json()["user"]["role"] == "member"

    self_demotion = client.patch(
        f"/api/admin/users/{admin_id}",
        headers=CSRF_HEADERS,
        json={"role": "member"},
    )
    assert self_demotion.status_code == 400

    client.post("/api/auth/logout")
    member_login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": initial_password},
    )
    assert member_login.status_code == 200
    assert client.get("/api/admin/users").status_code == 403

    wrong_current = client.post(
        "/api/user/change-password",
        headers=CSRF_HEADERS,
        json={"current_password": "wrong-password", "new_password": "member-password-v2"},
    )
    assert wrong_current.status_code == 400

    changed = client.post(
        "/api/user/change-password",
        headers=CSRF_HEADERS,
        json={"current_password": initial_password, "new_password": "member-password-v2"},
    )
    assert changed.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "alice", "password": initial_password},
    ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "member-password-v2"},
    ).status_code == 200

    admin_client = TestClient(main.app)
    assert admin_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret-pass"},
    ).status_code == 200

    rejected_reset = admin_client.post(
        f"/api/admin/users/{member_id}/reset-password",
        headers={"Origin": "https://evil.example"},
        json={"confirm": True},
    )
    assert rejected_reset.status_code == 403

    wrong_scheme = admin_client.post(
        f"https://testserver/api/admin/users/{member_id}/reset-password",
        headers={"Origin": "http://testserver"},
        json={"confirm": True},
    )
    assert wrong_scheme.status_code == 403

    reset = admin_client.post(
        f"/api/admin/users/{member_id}/reset-password",
        headers=CSRF_HEADERS,
        json={"confirm": True},
    )
    assert reset.status_code == 200
    reset_password = reset.json()["temporary_password"]
    assert len(reset_password) >= 20
    assert "password_hash" not in reset.text
    assert initial_password not in str(main.auth_service.list_audit_logs())
    assert reset_password not in str(main.auth_service.list_audit_logs())

    assert client.get("/api/auth/me").status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "member-password-v2"},
    ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "alice", "password": reset_password},
    ).status_code == 200


def test_only_builtin_admin_can_modify_or_reset_admin_accounts(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    owner_client = TestClient(main.app)

    owner_client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "owner-password", "display_name": "系统管理员"},
    )
    operator = owner_client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "opsadmin",
            "password": "operator-password",
            "display_name": "运维管理员",
            "role": "admin",
        },
    ).json()["user"]
    target_admin = owner_client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "targetadmin",
            "password": "target-password",
            "display_name": "目标管理员",
            "role": "admin",
        },
    ).json()["user"]
    member = owner_client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "member1",
            "password": "member-password",
            "display_name": "普通用户",
            "role": "member",
        },
    ).json()["user"]

    owner_update = owner_client.patch(
        f"/api/admin/users/{target_admin['id']}",
        headers=CSRF_HEADERS,
        json={"display_name": "目标管理员-已更新"},
    )
    assert owner_update.status_code == 200
    assert owner_client.post(
        f"/api/admin/users/{target_admin['id']}/reset-password",
        headers=CSRF_HEADERS,
        json={"confirm": True},
    ).status_code == 200

    operator_client = TestClient(main.app)
    assert operator_client.post(
        "/api/auth/login",
        json={"username": operator["username"], "password": "operator-password"},
    ).status_code == 200

    forbidden_update = operator_client.patch(
        f"/api/admin/users/{target_admin['id']}",
        headers=CSRF_HEADERS,
        json={"display_name": "越权修改"},
    )
    assert forbidden_update.status_code == 403
    assert forbidden_update.json()["detail"] == "仅内置 admin 可管理管理员账号"

    forbidden_reset = operator_client.post(
        f"/api/admin/users/{target_admin['id']}/reset-password",
        headers=CSRF_HEADERS,
        json={"confirm": True},
    )
    assert forbidden_reset.status_code == 403
    assert forbidden_reset.json()["detail"] == "仅内置 admin 可管理管理员账号"

    member_update = operator_client.patch(
        f"/api/admin/users/{member['id']}",
        headers=CSRF_HEADERS,
        json={"display_name": "普通用户-已更新"},
    )
    assert member_update.status_code == 200
    assert operator_client.post(
        f"/api/admin/users/{member['id']}/reset-password",
        headers=CSRF_HEADERS,
        json={"confirm": True},
    ).status_code == 200


def test_admin_management_rechecks_target_role_inside_write_transaction(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    owner_client = TestClient(main.app)
    owner_client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "owner-password", "display_name": "系统管理员"},
    )
    operator = owner_client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "opsadmin",
            "password": "operator-password",
            "display_name": "运维管理员",
            "role": "admin",
        },
    ).json()["user"]
    profile_target = owner_client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "profiletarget",
            "password": "profile-password",
            "display_name": "待提升用户",
            "role": "member",
        },
    ).json()["user"]
    password_target = owner_client.post(
        "/api/admin/users",
        headers=CSRF_HEADERS,
        json={
            "username": "passwordtarget",
            "password": "password-before-reset",
            "display_name": "待提升密码用户",
            "role": "member",
        },
    ).json()["user"]

    operator_client = TestClient(main.app)
    assert operator_client.post(
        "/api/auth/login",
        json={"username": operator["username"], "password": "operator-password"},
    ).status_code == 200

    original_get_user = main.auth_service.get_user_by_id
    original_update_profile = main.auth_service.update_user_profile
    promote_before_write = {profile_target["id"], password_target["id"]}

    def get_user_then_promote(user_id):
        snapshot = original_get_user(user_id)
        if user_id in promote_before_write:
            promote_before_write.remove(user_id)
            original_update_profile(user_id, role="admin")
        return snapshot

    monkeypatch.setattr(main.auth_service, "get_user_by_id", get_user_then_promote)

    forbidden_update = operator_client.patch(
        f"/api/admin/users/{profile_target['id']}",
        headers=CSRF_HEADERS,
        json={"display_name": "竞态越权修改"},
    )
    assert forbidden_update.status_code == 403
    assert original_get_user(profile_target["id"])["display_name"] == "待提升用户"

    forbidden_reset = operator_client.post(
        f"/api/admin/users/{password_target['id']}/reset-password",
        headers=CSRF_HEADERS,
        json={"confirm": True},
    )
    assert forbidden_reset.status_code == 403
    assert main.auth_service.authenticate("passwordtarget", "password-before-reset") is not None
