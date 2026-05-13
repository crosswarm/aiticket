"""aiticket skill 脚本共用的 API 客户端。"""

from __future__ import annotations

import sys
from typing import Any

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

from profile import get_active_profile


class AITicketClient:
    def __init__(self, profile_name: str | None = None) -> None:
        cfg = get_active_profile(profile_name)
        self._base = cfg["api_base"].rstrip("/")
        self._token = cfg.get("token", "")
        self._default_project = cfg.get("default_project", "")

    @property
    def default_project(self) -> str:
        return self._default_project

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def get(self, path: str, **params: Any) -> Any:
        url = f"{self._base}{path}"
        r = requests.get(url, headers=self._headers(), params=params, timeout=30)
        self._raise(r)
        return r.json()

    def post(self, path: str, body: dict | None = None) -> Any:
        url = f"{self._base}{path}"
        r = requests.post(url, headers=self._headers(), json=body or {}, timeout=60)
        self._raise(r)
        return r.json()

    def post_multipart(self, path: str, files: dict, data: dict | None = None) -> Any:
        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        r = requests.post(url, headers=headers, files=files, data=data or {}, timeout=120)
        self._raise(r)
        return r.json()

    def _raise(self, r: requests.Response) -> None:
        if r.status_code == 401:
            print("ERROR: Token 无效或已过期，请重新运行 /aiticket-profile-add")
            sys.exit(1)
        if not r.ok:
            print(f"ERROR: {r.status_code} {r.text[:200]}")
            sys.exit(1)
