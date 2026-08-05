import os
import pytest

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
    """两个 env 必须同时存在，否则容器重建后 Jira/PM/LLM 密文无法解密。

    注意：容器内跑测试时读不到这个文件 —— bind-mount 只映射 <repo>/APP，
    仓库根的 docker-compose.yml 不在容器里。那种情况下应明确 skip 并说明原因，
    而不是报 FileNotFoundError：一个因环境而假失败的测试，会训练人忽略真失败。
    （容器的实际 env 是否正确，用 `docker inspect` 核，不该由这个测试负责。）
    """
    compose_path = os.path.join(os.path.dirname(FRONTEND_DIR), "..", "docker-compose.yml")
    if not os.path.exists(compose_path):
        pytest.skip(f"docker-compose.yml 不在当前运行环境内（{compose_path}）；"
                    "容器内跑测试属正常，请在宿主仓库目录验证")

    compose_source = open(compose_path, encoding="utf-8").read()

    assert "APP_AUTH_DB_PATH=/data/app_auth.db" in compose_source
    assert "APP_AUTH_SECRET_PATH=/data/app_auth.key" in compose_source
