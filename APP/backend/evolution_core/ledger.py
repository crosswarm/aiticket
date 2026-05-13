"""
Ledger：Darwin 进化轮次记录
TSV 格式，append-only，存于 conclusion/_local/evolution/<module>/ledger.tsv
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from APP.backend.evolution_core.constants import EVOLUTION_DIR

# TSV 列定义（顺序固定，不可随意增减）
COLUMNS = [
    "round_id",
    "timestamp",
    "module",
    "parent_version",
    "child_version",
    "genome_hash",
    "slot_mutated",
    "fitness_composite",
    "kept",
    "revert_reason",
    "notes",
]


class Ledger:
    def __init__(self, module_id: str) -> None:
        self.module_id = module_id
        root = Path.cwd()
        ledger_dir = root / EVOLUTION_DIR / module_id
        ledger_dir.mkdir(parents=True, exist_ok=True)
        self.path = ledger_dir / "ledger.tsv"
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter="\t")
                writer.writerow(COLUMNS)

    def append(
        self,
        round_id: str,
        parent_version: str,
        child_version: str,
        genome_hash: str,
        slot_mutated: str,
        fitness_composite: float,
        kept: bool,
        revert_reason: str = "",
        notes: str = "",
    ) -> None:
        import time

        row = {
            "round_id": round_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "module": self.module_id,
            "parent_version": parent_version,
            "child_version": child_version,
            "genome_hash": genome_hash,
            "slot_mutated": slot_mutated,
            "fitness_composite": f"{fitness_composite:.4f}",
            "kept": "1" if kept else "0",
            "revert_reason": revert_reason,
            "notes": notes,
        }
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t")
            writer.writerow(row)

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            reader = list(csv.DictReader(f, delimiter="\t"))
        return reader[-n:]

    def best_by_dim(self, dim: str) -> dict[str, Any]:
        """
        Return the row with highest fitness_composite where slot_mutated == dim.
        """
        rows = []
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                if row.get("slot_mutated") == dim:
                    rows.append(row)
        if not rows:
            return {}
        return max(rows, key=lambda r: float(r.get("fitness_composite", 0) or 0))
