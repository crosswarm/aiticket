from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional

from .base import GenericIssue

_BASE_DIR = Path(__file__).parent.parent


class IssueRepository:
    """Per-source JSON cache for non-Jira providers.

    Jira issues continue to use JiraService.save/load_board_cache().
    This repository is only activated when issue_provider.name != 'jira'.

    Writes use write-then-rename (os.replace) for atomic updates so a
    concurrent reader never sees a partial file.
    """

    def __init__(self, cache_dir: str | None = None):
        default = _BASE_DIR / "data" / "cache"
        self._dir = Path(cache_dir) if cache_dir else default
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, source: str) -> Path:
        safe = source.replace(":", "_").replace("/", "_")
        return self._dir / f"{safe}_board.json"

    def save(self, source: str, issues: List[GenericIssue]) -> None:
        data = [vars(i) for i in issues]
        tmp = self._path(source).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=None))
        os.replace(tmp, self._path(source))

    def load(self, source: str) -> List[GenericIssue]:
        p = self._path(source)
        if not p.exists():
            return []
        try:
            raw = json.loads(p.read_text())
            return [GenericIssue(**d) for d in raw]
        except Exception:
            return []

    def get_by_key(self, key: str) -> Optional[GenericIssue]:
        if ":" not in key:
            return None
        source = key.split(":")[0]
        for issue in self.load(source):
            if issue.key == key:
                return issue
        return None

    def get_cache_info(self, source: str) -> dict:
        p = self._path(source)
        if not p.exists():
            return {"exists": False, "age_sec": 9999}
        age = time.time() - p.stat().st_mtime
        return {"exists": True, "age_sec": age}
