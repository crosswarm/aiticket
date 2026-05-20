from __future__ import annotations

import os
from typing import Any, List, Optional

import requests as _requests

from .base import GenericIssue


class GenericAPIIssueProvider:
    """Import issues from any JSON REST API via declarative config.

    Configure via deployment.yaml:

        data_source:
          type: api
          api:
            base_url: "https://support.example.com"
            auth:
              type: bearer          # basic | bearer | apikey
              token_env: "SUPPORT_API_TOKEN"
            endpoints:
              list: "/api/v2/tickets"
              detail: "/api/v2/tickets/{id}"   # optional
            pagination:
              style: page           # page | offset | none
              page_param: page
              size_param: per_page
              size: 100
            field_map:
              external_id: "$.id"
              summary: "$.subject"
              description: "$.description"
              status: "$.status"
              assignee: "$.assignee_id"
              created: "$.created_at"
              updated: "$.updated_at"
            items_key: "$.tickets"  # JSON path to the list within the response

    Paths support simple dot-notation: "$.fields.summary" walks
    obj["fields"]["summary"].  Top-level "$.key" maps to obj["key"].
    """

    name = "api"

    def __init__(self, config: dict):
        self.base_url    = config["base_url"].rstrip("/")
        self._auth_cfg   = config.get("auth", {})
        self.list_path   = config["endpoints"]["list"]
        self.detail_path = config["endpoints"].get("detail", "")
        self.field_map   = config.get("field_map", {})
        self.pagination  = config.get("pagination", {
            "style": "page", "page_param": "page",
            "size_param": "per_page", "size": 100,
        })
        self._items_key  = config.get("items_key", "")
        self._timeout    = int(config.get("timeout", 30))

    # ------------------------------------------------------------------
    # IssueProvider protocol
    # ------------------------------------------------------------------

    def fetch_all(self, filter_spec: dict | None = None) -> List[GenericIssue]:
        pag   = self.pagination
        style = pag.get("style", "page")
        size  = int(pag.get("size", 100))
        issues: List[GenericIssue] = []
        page  = 1

        while True:
            params: dict = {}
            if style == "page":
                params[pag.get("page_param", "page")] = page
                params[pag.get("size_param", "per_page")] = size
            elif style == "offset":
                params[pag.get("offset_param", "offset")] = (page - 1) * size
                params[pag.get("size_param", "limit")] = size
            if filter_spec:
                params.update(filter_spec)

            try:
                r = _requests.get(
                    f"{self.base_url}{self.list_path}",
                    headers=self._headers(),
                    auth=self._basic_auth(),
                    params=params,
                    timeout=self._timeout,
                )
                r.raise_for_status()
            except Exception as e:
                print(f"[APIProvider] fetch failed (page {page}): {e}")
                break

            items = self._get_items(r.json())
            if not items:
                break

            for item in items:
                issue = self._to_generic(item)
                if issue:
                    issues.append(issue)

            if style == "none" or len(items) < size:
                break
            page += 1

        print(f"[APIProvider] fetched {len(issues)} issues from {self.base_url}")
        return issues

    def fetch_incremental(self, since: str) -> List[GenericIssue]:
        # Pass since as a query param; the exact param name depends on the API.
        return self.fetch_all(filter_spec={"updated_since": since})

    def get_issue(self, key: str) -> Optional[GenericIssue]:
        ext_id = key.removeprefix("api:")
        if not self.detail_path:
            return None
        try:
            r = _requests.get(
                f"{self.base_url}{self.detail_path.format(id=ext_id)}",
                headers=self._headers(),
                auth=self._basic_auth(),
                timeout=self._timeout,
            )
            r.raise_for_status()
            return self._to_generic(r.json())
        except Exception:
            return None

    def health_check(self) -> dict:
        try:
            r = _requests.get(
                f"{self.base_url}{self.list_path}",
                headers=self._headers(),
                auth=self._basic_auth(),
                params={self.pagination.get("size_param", "per_page"): 1},
                timeout=5,
            )
            return {"ok": r.ok, "status_code": r.status_code, "source": "api"}
        except Exception as e:
            return {"ok": False, "error": str(e), "source": "api"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        auth = self._auth_cfg
        t = auth.get("type", "")
        if t == "bearer":
            token = os.environ.get(auth.get("token_env", ""), auth.get("token", ""))
            return {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if t == "apikey":
            key_val = os.environ.get(auth.get("key_env", ""), "")
            header_name = auth.get("header", "X-API-Key")
            return {header_name: key_val, "Accept": "application/json"}
        return {"Accept": "application/json"}

    def _basic_auth(self):
        auth = self._auth_cfg
        if auth.get("type") == "basic":
            user = os.environ.get(auth.get("username_env", ""), auth.get("username", ""))
            pw   = os.environ.get(auth.get("password_env", ""), auth.get("password", ""))
            return (user, pw) if user else None
        return None

    def _extract(self, obj: Any, path: str) -> Any:
        if not path:
            return ""
        # Strip leading "$." or "$"
        clean = path.lstrip("$").lstrip(".")
        if not clean:
            return obj
        parts = clean.split(".")
        cur = obj
        for p in parts:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(p, "")
        return cur

    def _get_items(self, resp_json: Any) -> list:
        if self._items_key:
            result = self._extract(resp_json, self._items_key)
            return result if isinstance(result, list) else []
        if isinstance(resp_json, list):
            return resp_json
        # Auto-detect: return first list value in the response dict
        if isinstance(resp_json, dict):
            for v in resp_json.values():
                if isinstance(v, list):
                    return v
        return []

    def _to_generic(self, item: dict) -> Optional[GenericIssue]:
        ext_id = str(self._extract(item, self.field_map.get("external_id", "$.id")) or "").strip()
        if not ext_id:
            return None
        return GenericIssue(
            source="api",
            external_id=ext_id,
            key=f"api:{ext_id}",
            summary=str(self._extract(item, self.field_map.get("summary", "")) or ""),
            description=str(self._extract(item, self.field_map.get("description", "")) or ""),
            status=str(self._extract(item, self.field_map.get("status", "")) or ""),
            assignee=str(self._extract(item, self.field_map.get("assignee", "")) or ""),
            reporter=str(self._extract(item, self.field_map.get("reporter", "")) or ""),
            created=str(self._extract(item, self.field_map.get("created", "")) or ""),
            updated=str(self._extract(item, self.field_map.get("updated", "")) or ""),
            priority=str(self._extract(item, self.field_map.get("priority", "")) or ""),
            issue_type=str(self._extract(item, self.field_map.get("issue_type", "")) or ""),
            project_name=str(self._extract(item, self.field_map.get("project_name", "")) or ""),
            extra=item,
        )
