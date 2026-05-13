import os
import sys
import importlib

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("APP_AUTH_SECRET_PATH", str(tmp_path / "auth.key"))
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    return importlib.reload(main)


def bootstrap_and_login(client):
    client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "secret-pass", "display_name": "管理员"},
    )


def test_export_report_passes_app_base_url(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)

    captured = {}

    class StubExportService:
        def start_export(self, report_type, report_id, formats, app_base_url=None):
            captured["report_type"] = report_type
            captured["report_id"] = report_id
            captured["formats"] = formats
            captured["app_base_url"] = app_base_url
            return "export-task-1"

    monkeypatch.setattr(main, "export_service", StubExportService())

    client = TestClient(main.app)
    bootstrap_and_login(client)

    response = client.post(
        "/api/export/report",
        json={
            "report_type": "weekly",
            "report_id": "Weekly_Report_2026-03-09_2026-03-15.json",
            "formats": ["pdf"],
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "export-task-1"
    assert captured["report_type"] == "weekly"
    assert captured["report_id"] == "Weekly_Report_2026-03-09_2026-03-15.json"
    assert captured["formats"] == ["pdf"]
    assert captured["app_base_url"] == "http://testserver"
