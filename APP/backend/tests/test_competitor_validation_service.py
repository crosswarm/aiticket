import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class ImmediateThread:
    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def test_validation_service_public_capture_completes_with_fake_runner(tmp_path, monkeypatch):
    import competitor_validation_service as validation_module
    from competitor_validation_service import CompetitorValidationService

    monkeypatch.setattr(validation_module.threading, "Thread", ImmediateThread)

    def fake_capture(task, payload, storage_state_path=None):
        screenshot_path = tmp_path / "public.png"
        screenshot_path.write_bytes(b"fake-image")
        return {
            "captures": [
                {
                    "id": "cap-1",
                    "title": "公开页面整页截图",
                    "file_path": str(screenshot_path),
                    "capture_type": "page",
                }
            ],
            "notes": ["公开页面已截图"],
        }

    service = CompetitorValidationService(
        data_dir=str(tmp_path),
        capture_runner=fake_capture,
        command_runner=lambda *args, **kwargs: None,
    )

    task_id = service.start_validation(
        "REQ-MYPROJECT-59346",
        {
            "vendor": "SAP",
            "target_url": "https://help.sap.com/example",
            "capture_mode": "public_capture",
            "profile_name": "",
            "focus_hint": "流程监控图",
        },
    )

    task = service.get_task_status(task_id)
    assert task["status"] == "completed"
    assert task["result"]["captures"][0]["title"] == "公开页面整页截图"
    assert task["result"]["verification"]["status"] == "verified_public"


def test_validation_service_authenticated_capture_requires_manual_login_when_profile_missing(tmp_path, monkeypatch):
    import competitor_validation_service as validation_module
    from competitor_validation_service import CompetitorValidationService

    monkeypatch.setattr(validation_module.threading, "Thread", ImmediateThread)

    service = CompetitorValidationService(
        data_dir=str(tmp_path),
        capture_runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("capture should not run")),
        command_runner=lambda *args, **kwargs: None,
    )

    task_id = service.start_validation(
        "REQ-MYPROJECT-59346",
        {
            "vendor": "SAP",
            "target_url": "https://help.sap.com/example",
            "capture_mode": "authenticated_capture",
            "profile_name": "sap-default",
            "focus_hint": "审批面板",
        },
    )

    task = service.get_task_status(task_id)
    assert task["status"] == "requires_manual_login"
    assert "sap-default" in task["manual_login_hint"]


def test_validation_service_reports_tool_health_from_local_environment(tmp_path):
    from competitor_validation_service import CompetitorValidationService

    class FakeCompletedProcess:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_runner(args, **kwargs):
        if args[:2] == ["agent-reach", "doctor"]:
            return FakeCompletedProcess(stdout="状态：6/13 个渠道可用\n✅ 全网语义搜索")
        return FakeCompletedProcess(stdout="", returncode=0)

    service = CompetitorValidationService(data_dir=str(tmp_path), command_runner=fake_runner)
    health = service.get_tool_health()

    assert health["agent_reach"]["available"] is True
    assert health["agent_reach"]["channels_ready"] == 6
    assert health["playwright"]["available"] is True
    assert health["agent_browser"]["available"] is True
    assert health["firecrawl"]["available"] is False
