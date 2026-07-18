from concurrent.futures import ThreadPoolExecutor
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth_service import AuthService


def test_auth_service_bootstrap_authenticate_and_session_roundtrip(tmp_path):
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
        session_ttl_hours=8,
    )

    assert service.has_users() is False

    admin = service.bootstrap_admin("admin", "secret-pass", display_name="管理员")
    authenticated = service.authenticate("admin", "secret-pass")
    session_token = service.create_session(admin["id"])
    current_user = service.get_user_by_session(session_token)

    assert service.has_users() is True
    assert authenticated["username"] == "admin"
    assert current_user["id"] == admin["id"]
    assert current_user["role"] == "admin"


def test_auth_service_encrypts_and_roundtrips_jira_binding(tmp_path):
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
        session_ttl_hours=8,
    )
    admin = service.bootstrap_admin("admin", "secret-pass", display_name="管理员")
    member = service.create_user("alice", "member-pass", display_name="Alice", role="member", created_by=admin["id"])

    service.upsert_jira_binding(
        member["id"],
        jira_username="alice.jira",
        jira_api_token="jira-secret-token",
        jira_base_url="https://jira.example.com",
    )

    summary = service.get_jira_binding_summary(member["id"])
    credentials = service.get_jira_binding_credentials(member["id"])

    assert summary["jira_username"] == "alice.jira"
    assert summary["has_token"] is True
    assert "jira-secret-token" not in str(summary)
    assert credentials["jira_username"] == "alice.jira"
    assert credentials["jira_api_token"] == "jira-secret-token"
    assert credentials["jira_base_url"] == "https://jira.example.com"


def test_user_profile_and_password_updates_preserve_expected_state(tmp_path):
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
        session_ttl_hours=8,
    )
    admin = service.bootstrap_admin("admin", "secret-pass", display_name="管理员")
    member = service.create_user(
        "alice",
        "member-pass",
        display_name="Alice",
        role="member",
        created_by=admin["id"],
    )

    updated = service.update_user_profile(member["id"], display_name="爱丽丝")

    assert updated["display_name"] == "爱丽丝"
    assert updated["role"] == "member"
    assert updated["is_active"] is True
    assert service.authenticate("alice", "member-pass")["id"] == member["id"]

    first_session = service.create_session(member["id"])
    second_session = service.create_session(member["id"])
    device_token = service.issue_device_token("alice", "member-pass", "device-one")
    skill_token = service.create_skill_token(member["id"])["token"]
    service.change_password(member["id"], "member-pass", "new-member-pass")

    assert service.authenticate("alice", "member-pass") is None
    assert service.authenticate("alice", "new-member-pass")["id"] == member["id"]
    assert service.get_user_by_session(first_session) is None
    assert service.get_user_by_session(second_session) is None
    assert service.verify_device_token(device_token, "device-one") is None
    assert service.get_user_by_skill_token(skill_token) is None

    reset_session = service.create_session(member["id"])
    reset_device_token = service.issue_device_token("alice", "new-member-pass", "device-two")
    reset_skill_token = service.create_skill_token(member["id"])["token"]
    service.reset_password(member["id"], "reset-member-pass")

    assert service.authenticate("alice", "new-member-pass") is None
    assert service.authenticate("alice", "reset-member-pass")["id"] == member["id"]
    assert service.get_user_by_session(reset_session) is None
    assert service.verify_device_token(reset_device_token, "device-two") is None
    assert service.get_user_by_skill_token(reset_skill_token) is None


def test_disabling_and_reenabling_user_does_not_restore_credentials(tmp_path):
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
    )
    admin = service.bootstrap_admin("admin", "secret-pass")
    member = service.create_user("alice", "member-pass", created_by=admin["id"])
    session_token = service.create_session(member["id"])
    device_token = service.issue_device_token("alice", "member-pass", "device-one")
    skill_token = service.create_skill_token(member["id"])["token"]

    service.update_user_profile(member["id"], is_active=False)
    service.update_user_profile(member["id"], is_active=True)

    assert service.get_user_by_session(session_token) is None
    assert service.verify_device_token(device_token, "device-one") is None
    assert service.get_user_by_skill_token(skill_token) is None


def test_concurrent_admin_demotion_preserves_one_active_admin(tmp_path):
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
    )
    first = service.bootstrap_admin("admin-one", "secret-pass")
    second = service.create_user("admin-two", "secret-pass", role="admin")
    start = threading.Barrier(2)

    def demote(user_id):
        start.wait()
        try:
            service.update_user_profile(user_id, role="member")
            return "updated"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, (first["id"], second["id"])))

    assert outcomes.count("updated") == 1
    assert outcomes.count("At least one active admin is required") == 1
    active_admins = [user for user in service.list_users() if user["role"] == "admin" and user["is_active"]]
    assert len(active_admins) == 1
