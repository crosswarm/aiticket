import os

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")
FRONTEND_DIR = os.path.normpath(os.path.join(BACKEND_DIR, "../frontend"))


def test_kb_page_route_returns_existing_kb_html_file():
    kb_page_path = os.path.join(FRONTEND_DIR, "kb.html")

    assert kb_page_path.endswith("kb.html")
    assert os.path.exists(kb_page_path)


def test_settings_page_contains_password_and_admin_user_management_contracts():
    settings_page_path = os.path.join(FRONTEND_DIR, "settings.html")
    nav_path = os.path.join(FRONTEND_DIR, "assets", "nav.js")

    assert os.path.exists(settings_page_path)
    settings_source = open(settings_page_path, encoding="utf-8").read()
    nav_source = open(nav_path, encoding="utf-8").read()

    assert "/api/user/change-password" in settings_source
    assert "/api/admin/users" in settings_source
    assert "/reset-password" in settings_source
    assert "admin-user-management" in settings_source
    assert "settings.html" in nav_source
