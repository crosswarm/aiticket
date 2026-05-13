"""aiticket Skill 多服务器 Profile 管理。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SKILL_DIR = Path.home() / ".claude" / "skills" / "aiticket"
_PROFILES_PATH = _SKILL_DIR / "profiles.json"
_LEGACY_CONFIG_PATH = _SKILL_DIR / "config.json"


def _load_raw() -> dict[str, Any]:
    if _PROFILES_PATH.exists():
        with open(_PROFILES_PATH) as f:
            return json.load(f)
    return {"default": None, "profiles": {}}


def _save_raw(data: dict[str, Any]) -> None:
    _SKILL_DIR.mkdir(parents=True, exist_ok=True)
    with open(_PROFILES_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _maybe_migrate() -> None:
    """One-time migration from legacy config.json to profiles.json."""
    if _PROFILES_PATH.exists() or not _LEGACY_CONFIG_PATH.exists():
        return
    with open(_LEGACY_CONFIG_PATH) as f:
        legacy = json.load(f)
    data = {
        "default": "default",
        "profiles": {
            "default": {
                "api_base": legacy.get("api_base", ""),
                "token": legacy.get("token", ""),
                "default_project": legacy.get("default_project", ""),
            }
        },
    }
    _save_raw(data)
    _LEGACY_CONFIG_PATH.rename(_LEGACY_CONFIG_PATH.with_suffix(".json.bak"))
    print(f"[profile] Migrated config.json → profiles.json (backup: config.json.bak)")


def load_profiles() -> dict[str, Any]:
    _maybe_migrate()
    return _load_raw()


def get_active_profile(name: str | None = None) -> dict[str, Any]:
    """Return profile dict. Priority: arg > AITICKET_PROFILE env > default."""
    import os
    data = load_profiles()
    profiles = data.get("profiles", {})

    target = name or os.environ.get("AITICKET_PROFILE") or data.get("default")
    if not target:
        print("ERROR: No profile configured. Run /aiticket-profile-add <name> to set up.")
        sys.exit(1)
    if target not in profiles:
        print(f"ERROR: Profile '{target}' not found. Run /aiticket-profile-list to see available profiles.")
        sys.exit(1)
    return profiles[target]


def save_profile(name: str, api_base: str, token: str, default_project: str = "") -> None:
    data = load_profiles()
    data.setdefault("profiles", {})[name] = {
        "api_base": api_base.rstrip("/"),
        "token": token,
        "default_project": default_project,
    }
    if not data.get("default"):
        data["default"] = name
    _save_raw(data)


def set_default(name: str) -> None:
    data = load_profiles()
    if name not in data.get("profiles", {}):
        print(f"ERROR: Profile '{name}' not found.")
        sys.exit(1)
    data["default"] = name
    _save_raw(data)


def delete_profile(name: str) -> None:
    data = load_profiles()
    if name not in data.get("profiles", {}):
        print(f"ERROR: Profile '{name}' not found.")
        sys.exit(1)
    del data["profiles"][name]
    if data.get("default") == name:
        remaining = list(data["profiles"].keys())
        data["default"] = remaining[0] if remaining else None
    _save_raw(data)


def list_profiles() -> list[dict[str, Any]]:
    data = load_profiles()
    default = data.get("default")
    result = []
    for name, p in data.get("profiles", {}).items():
        token = p.get("token", "")
        masked = token[:6] + "..." + token[-4:] if len(token) > 12 else "****"
        result.append({
            "name": name,
            "api_base": p.get("api_base", ""),
            "token_masked": masked,
            "default_project": p.get("default_project", ""),
            "is_default": name == default,
        })
    return result
