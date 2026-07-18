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


def test_settings_sidebar_footer_controls_have_clickable_dependencies_and_button_reset():
    settings_page_path = os.path.join(FRONTEND_DIR, "settings.html")
    nav_path = os.path.join(FRONTEND_DIR, "assets", "nav.js")
    responsive_path = os.path.join(FRONTEND_DIR, "assets", "responsive.css")

    settings_source = open(settings_page_path, encoding="utf-8").read()
    nav_source = open(nav_path, encoding="utf-8").read()
    responsive_source = open(responsive_path, encoding="utf-8").read()

    assert "/assets/ds/command-palette.css" in settings_source
    assert "/assets/ds/command-palette.js" in settings_source
    assert "<script>const API_BASE" not in settings_source
    assert 'onclick="window.DSCommandPalette && window.DSCommandPalette.open()"' in nav_source
    assert 'onclick="window.DSLLMConfig && window.DSLLMConfig.open()"' in nav_source
    assert 'onclick="window.DSTheme && window.DSTheme.toggle()"' in nav_source
    assert nav_source.count('<button type="button" class="ds-sidebar-link') >= 3

    sidebar_link_rule = responsive_source.split("\n.ds-sidebar-link {", 1)[1].split("}", 1)[0]
    assert "border: 0" in sidebar_link_rule
    assert "background: transparent" in sidebar_link_rule
    assert "cursor: pointer" in sidebar_link_rule
    assert "text-align: left" in sidebar_link_rule

    assert "currentUser.username === 'admin' || user.role !== 'admin'" in settings_source
    assert "仅内置 admin 可管理" in settings_source
    assert '<td style="white-space:nowrap">\n                    <td style="white-space:nowrap">' not in settings_source


def test_docker_compose_persists_auth_database_and_encryption_key():
    compose_path = os.path.join(os.path.dirname(FRONTEND_DIR), "..", "docker-compose.yml")
    compose_source = open(compose_path, encoding="utf-8").read()

    assert "APP_AUTH_DB_PATH=/data/app_auth.db" in compose_source
    assert "APP_AUTH_SECRET_PATH=/data/app_auth.key" in compose_source
