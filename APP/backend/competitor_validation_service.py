from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "data_cache" / "competitor_validation"
DEFAULT_RUNNER_SCRIPT = BASE_DIR / "tools" / "competitor_validation_runner.js"
DEFAULT_ARTIFACT_DIR = PROJECT_ROOT / "design" / "spec" / ".draft_artifacts"


class CompetitorValidationService:
    def __init__(
        self,
        data_dir: str | None = None,
        capture_runner: Any | None = None,
        command_runner: Any | None = None,
        artifact_dir: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.capture_dir = self.data_dir / "captures"
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = self.data_dir / "profiles"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.command_runner = command_runner or subprocess.run
        self.capture_runner = capture_runner or self._run_playwright_capture
        self.artifact_dir = Path(artifact_dir or DEFAULT_ARTIFACT_DIR)
        self.tasks: dict[str, dict[str, Any]] = {}

    def start_validation(self, req_id: str, payload: dict[str, Any]) -> str:
        task_id = str(uuid.uuid4())
        normalized = {
            "draft_id": str(payload.get("draft_id") or "").strip(),
            "vendor": str(payload.get("vendor") or "").strip(),
            "target_url": str(payload.get("target_url") or "").strip(),
            "capture_mode": str(payload.get("capture_mode") or "public_capture").strip(),
            "profile_name": str(payload.get("profile_name") or "").strip(),
            "focus_hint": str(payload.get("focus_hint") or "").strip(),
            "steps": list(payload.get("steps") or []),
        }
        self.tasks[task_id] = {
            "task_id": task_id,
            "req_id": req_id,
            "status": "queued",
            "payload": normalized,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "manual_login_hint": "",
            "error": "",
            "result": {},
        }
        worker = threading.Thread(target=self._run_validation, args=(task_id,), daemon=True)
        worker.start()
        return task_id

    def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    def get_tool_health(self) -> dict[str, Any]:
        doctor_output = ""
        doctor_error = ""
        channels_ready = 0
        agent_reach_available = shutil.which("agent-reach") is not None

        if agent_reach_available:
            try:
                result = self.command_runner(
                    ["agent-reach", "doctor"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                doctor_output = getattr(result, "stdout", "") or ""
                doctor_error = getattr(result, "stderr", "") or ""
                if getattr(result, "returncode", 1) != 0:
                    doctor_error = doctor_error or "agent-reach doctor failed"
            except Exception as exc:
                doctor_error = str(exc)
        match = re.search(r"(\d+)\s*/\s*\d+", doctor_output)
        if match:
            channels_ready = int(match.group(1))

        return {
            "agent_reach": {
                "available": agent_reach_available,
                "channels_ready": channels_ready,
                "doctor_output": doctor_output.strip(),
                "error": doctor_error.strip(),
            },
            "playwright": {
                "available": bool(shutil.which("playwright") or shutil.which("npx")),
                "runner_script": str(DEFAULT_RUNNER_SCRIPT),
            },
            "agent_browser": {
                "available": shutil.which("agent-browser") is not None,
            },
            "firecrawl": {
                "available": shutil.which("firecrawl") is not None,
            },
        }

    def _run_validation(self, task_id: str) -> None:
        task = self.tasks[task_id]
        task["status"] = "running"
        task["updated_at"] = datetime.now().isoformat()

        payload = task["payload"]
        capture_mode = payload.get("capture_mode") or "public_capture"
        storage_state_path = ""
        if capture_mode == "authenticated_capture":
            storage_state_path = self._storage_state_path(payload.get("profile_name", ""))
            if not storage_state_path or not os.path.exists(storage_state_path):
                task["status"] = "requires_manual_login"
                task["manual_login_hint"] = (
                    f"未找到登录态 profile={payload.get('profile_name') or 'default'}。"
                    "请先用 Playwright / agent-browser 完成一次人工登录并保存 storage state。"
                )
                task["updated_at"] = datetime.now().isoformat()
                return

        try:
            result = self.capture_runner(task, payload, storage_state_path=storage_state_path)
            captures = list((result or {}).get("captures") or [])
            notes = list((result or {}).get("notes") or [])
            verification_status = "verified_public"
            if capture_mode == "authenticated_capture":
                verification_status = "verified_authenticated"

            task["result"] = {
                "verification": {
                    "status": verification_status,
                    "captures": captures,
                    "notes": notes,
                    "captured_at": datetime.now().isoformat(),
                },
                "captures": captures,
                "notes": notes,
            }
            task["status"] = "completed"
            task["updated_at"] = datetime.now().isoformat()
            self._write_back_verification(task)
        except Exception as exc:
            task["status"] = "failed"
            task["error"] = str(exc)
            task["updated_at"] = datetime.now().isoformat()

    def _run_playwright_capture(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
        storage_state_path: str | None = None,
    ) -> dict[str, Any]:
        if not DEFAULT_RUNNER_SCRIPT.exists():
            raise FileNotFoundError(f"Playwright runner script not found: {DEFAULT_RUNNER_SCRIPT}")

        runner_payload = {
            "task_id": task["task_id"],
            "target_url": payload.get("target_url", ""),
            "capture_mode": payload.get("capture_mode", "public_capture"),
            "storage_state_path": storage_state_path or "",
            "focus_hint": payload.get("focus_hint", ""),
            "steps": payload.get("steps", []),
            "output_dir": str(self.capture_dir),
        }

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as handle:
            json.dump(runner_payload, handle, ensure_ascii=False)
            payload_path = handle.name

        try:
            result = self.command_runner(
                ["node", str(DEFAULT_RUNNER_SCRIPT), payload_path],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        finally:
            try:
                os.unlink(payload_path)
            except OSError:
                pass

        if getattr(result, "returncode", 1) != 0:
            stderr = getattr(result, "stderr", "") or getattr(result, "stdout", "") or "capture failed"
            raise RuntimeError(stderr.strip())

        stdout = getattr(result, "stdout", "") or "{}"
        payload = json.loads(stdout)
        payload["captures"] = [self._normalize_capture(item) for item in payload.get("captures", []) or []]
        return payload

    def _normalize_capture(self, capture: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": capture.get("id") or str(uuid.uuid4()),
            "title": capture.get("title") or "竞品页面截图",
            "file_path": capture.get("file_path", ""),
            "capture_type": capture.get("capture_type", "page"),
        }

    def _storage_state_path(self, profile_name: str) -> str:
        name = (profile_name or "").strip()
        if not name:
            return ""
        return str(self.profile_dir / f"{name}.json")

    def _write_back_verification(self, task: dict[str, Any]) -> None:
        payload = task.get("payload", {}) or {}
        draft_id = payload.get("draft_id") or ""
        vendor_name = payload.get("vendor") or ""
        if not draft_id or not vendor_name:
            return

        artifact_path = self.artifact_dir / f"{draft_id}.json"
        if not artifact_path.exists():
            return

        try:
            with open(artifact_path, "r", encoding="utf-8") as handle:
                artifact = json.load(handle)
        except Exception:
            return

        competitor = artifact.get("competitor_comparison") or (artifact.get("analysis_packet", {}) or {}).get("competitor_comparison") or {}
        vendors = competitor.get("vendors", []) or []
        for vendor in vendors:
            if vendor.get("vendor") != vendor_name:
                continue
            vendor["verification"] = task["result"].get("verification", {})
            captures = list(task["result"].get("captures", []) or [])
            if captures:
                vendor.setdefault("evidence_items", [])
                for capture in captures:
                    vendor["evidence_items"].append(
                        {
                            "title": capture.get("title", ""),
                            "url": payload.get("target_url", ""),
                            "source_level": "verified_capture",
                            "snippet": payload.get("focus_hint", ""),
                        }
                    )
            break

        artifact["competitor_comparison"] = competitor
        artifact.setdefault("analysis_packet", {})["competitor_comparison"] = competitor
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                json.dump(artifact, handle, ensure_ascii=False, indent=2)
        except Exception:
            return
