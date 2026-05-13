"""Exploration Coverage Tracker — Phase 4 of exploration quality evolution.

Tracks competitive product exploration progress across a vendor x feature matrix.
Provides dashboard, staleness detection, and next-target suggestions.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent

COVERAGE_PATH = PROJECT_ROOT / "data_cache" / "competitor_validation" / "exploration_coverage.yaml"

VENDORS = ["kingdee", "sap", "weaver", "seeyon"]
FEATURES = [
    "workflow_approval", "delegation", "form_design", "branch_condition",
    "notification", "print_integration", "mobile_approval",
    "api_integration", "performance_monitor", "signature",
]

# Chinese labels for dashboard display
VENDOR_LABELS = {
    "kingdee": "金蝶",
    "sap": "SAP",
    "weaver": "泛微",
    "seeyon": "致远",
}

FEATURE_LABELS = {
    "workflow_approval": "审批流程",
    "delegation": "委托代理",
    "form_design": "表单设计",
    "branch_condition": "分支条件",
    "notification": "消息通知",
    "print_integration": "打印集成",
    "mobile_approval": "移动审批",
    "api_integration": "API集成",
    "performance_monitor": "流程监控",
    "signature": "电子签名",
}

# Business value weights for prioritization (higher = more important)
FEATURE_BUSINESS_VALUE = {
    "workflow_approval": 10,
    "delegation": 7,
    "form_design": 9,
    "branch_condition": 8,
    "notification": 6,
    "print_integration": 4,
    "mobile_approval": 8,
    "api_integration": 9,
    "performance_monitor": 5,
    "signature": 3,
}

# Depth mapping from feature_matrix support_status
STATUS_TO_DEPTH = {
    "full": "deep",
    "partial": "partial",
    "none": "shallow",  # explored but found nothing
}


class ExplorationCoverageTracker:
    """Track exploration coverage across vendor x feature matrix."""

    def __init__(self, coverage_path: str | Path | None = None):
        self.coverage_path = Path(coverage_path or COVERAGE_PATH)
        self.data = self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """Load coverage YAML, create skeleton if not exists."""
        if self.coverage_path.exists():
            with self.coverage_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
                if data and isinstance(data, dict):
                    return data
        return self._create_skeleton()

    def _create_skeleton(self) -> dict:
        """Create empty coverage matrix."""
        matrix: dict[str, dict[str, dict]] = {}
        for vendor in VENDORS:
            matrix[vendor] = {}
            for feature in FEATURES:
                matrix[vendor][feature] = {
                    "status": "unexplored",
                    "depth": "none",
                    "last_explored": None,
                    "kb_doc_count": 0,
                    "kb_total_chars": 0,
                    "feature_matrix_exists": False,
                    "confidence": "none",
                    "quality_scores": {},
                }
        return {
            "version": 1,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "matrix": matrix,
        }

    def save(self):
        """Save coverage YAML to disk."""
        self.coverage_path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now().isoformat()
        with self.coverage_path.open("w", encoding="utf-8") as fh:
            yaml.dump(
                self.data, fh,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )

    # ------------------------------------------------------------------
    # Cell accessors
    # ------------------------------------------------------------------

    def get_cell(self, vendor_id: str, feature_id: str) -> dict:
        """Get status of a specific vendor x feature cell."""
        matrix = self.data.get("matrix", {})
        vendor_data = matrix.get(vendor_id, {})
        cell = vendor_data.get(feature_id, {})
        if not cell:
            cell = {
                "status": "unexplored",
                "depth": "none",
                "last_explored": None,
                "kb_doc_count": 0,
                "kb_total_chars": 0,
                "feature_matrix_exists": False,
                "confidence": "none",
                "quality_scores": {},
            }
        # Compute freshness
        cell["freshness"] = self._compute_freshness(cell.get("last_explored"))
        return cell

    def update_cell(self, vendor_id: str, feature_id: str, result: dict):
        """Update a cell after exploration."""
        matrix = self.data.setdefault("matrix", {})
        vendor_data = matrix.setdefault(vendor_id, {})

        existing = vendor_data.get(feature_id, {})
        existing.update(result)

        # Auto-derive status from depth
        depth = existing.get("depth", "none")
        if depth == "deep":
            existing["status"] = "explored"
        elif depth == "partial":
            existing["status"] = "partial"
        elif depth == "shallow":
            existing["status"] = "partial"
        else:
            existing["status"] = "unexplored"

        if "last_explored" not in existing or not existing["last_explored"]:
            if depth != "none":
                existing["last_explored"] = datetime.now().isoformat()

        vendor_data[feature_id] = existing

    # ------------------------------------------------------------------
    # Summary and analytics
    # ------------------------------------------------------------------

    def get_coverage_summary(self) -> dict:
        """Return summary counts: total, explored, partial, unexplored, stale, expired."""
        total = len(VENDORS) * len(FEATURES)
        explored = 0
        partial = 0
        unexplored = 0
        stale = 0
        expired = 0

        for vendor in VENDORS:
            for feature in FEATURES:
                cell = self.get_cell(vendor, feature)
                status = cell.get("status", "unexplored")
                freshness = cell.get("freshness", "none")

                if status == "explored":
                    explored += 1
                elif status == "partial":
                    partial += 1
                else:
                    unexplored += 1

                if freshness == "stale":
                    stale += 1
                elif freshness == "expired":
                    expired += 1

        return {
            "total": total,
            "explored": explored,
            "partial": partial,
            "unexplored": unexplored,
            "stale": stale,
            "expired": expired,
            "coverage_pct": round((explored + partial) / total * 100, 1) if total else 0,
        }

    def get_stale_cells(self, max_age_days: int = 30) -> list:
        """Return cells that haven't been refreshed within max_age_days."""
        stale = []
        cutoff = datetime.now() - timedelta(days=max_age_days)
        for vendor in VENDORS:
            for feature in FEATURES:
                cell = self.get_cell(vendor, feature)
                last = cell.get("last_explored")
                if last and isinstance(last, str):
                    try:
                        last_dt = datetime.fromisoformat(last)
                        if last_dt < cutoff:
                            stale.append({
                                "vendor": vendor,
                                "feature": feature,
                                "last_explored": last,
                                "age_days": (datetime.now() - last_dt).days,
                            })
                    except (ValueError, TypeError):
                        pass
        return stale

    def get_dashboard(self) -> str:
        """Return a text dashboard for display."""
        # Column widths
        feat_w = 10  # feature label column
        cell_w = 8   # each vendor cell

        lines = []
        # Header
        sep_top = "+" + "-" * (feat_w + 2) + "+" + (("-" * (cell_w + 2) + "+") * len(VENDORS))
        header = (
            "| " + "功能\\竞品".ljust(feat_w) + " | "
            + " | ".join(VENDOR_LABELS[v].center(cell_w) for v in VENDORS)
            + " |"
        )
        sep_mid = "+" + "-" * (feat_w + 2) + "+" + (("-" * (cell_w + 2) + "+") * len(VENDORS))

        lines.append(sep_top)
        lines.append(header)
        lines.append(sep_mid)

        # Rows
        for feature in FEATURES:
            label = FEATURE_LABELS.get(feature, feature)[:feat_w].ljust(feat_w)
            cells = []
            for vendor in VENDORS:
                cell = self.get_cell(vendor, feature)
                depth = cell.get("depth", "none")
                confidence = cell.get("confidence", "none")
                icon = self._depth_icon(depth)
                text = f"{icon}{depth}"
                cells.append(text.center(cell_w))
            row = "| " + label + " | " + " | ".join(cells) + " |"
            lines.append(row)

        lines.append(sep_mid)

        # Summary line
        summary = self.get_coverage_summary()
        lines.append(
            f"覆盖率: {summary['coverage_pct']}% "
            f"({summary['explored']}深+{summary['partial']}浅) / {summary['total']}总  "
            f"未探索: {summary['unexplored']}  过期: {summary['stale']+summary['expired']}"
        )

        return "\n".join(lines)

    def suggest_next(self, n: int = 3) -> list:
        """Suggest next exploration targets (for user reference, NOT auto-execute).

        Priority: unexplored > stale > partial
        Within same priority: higher business_value features first
        """
        candidates = []
        for vendor in VENDORS:
            for feature in FEATURES:
                cell = self.get_cell(vendor, feature)
                status = cell.get("status", "unexplored")
                freshness = cell.get("freshness", "none")
                bv = FEATURE_BUSINESS_VALUE.get(feature, 5)

                # Assign priority score (lower = higher priority)
                if status == "unexplored":
                    priority = 0
                elif freshness in ("stale", "expired"):
                    priority = 1
                elif status == "partial":
                    priority = 2
                else:
                    priority = 3  # already explored and fresh

                if priority >= 3:
                    continue  # skip already-good cells

                candidates.append({
                    "vendor": vendor,
                    "vendor_name": VENDOR_LABELS.get(vendor, vendor),
                    "feature": feature,
                    "feature_name": FEATURE_LABELS.get(feature, feature),
                    "priority": priority,
                    "business_value": bv,
                    "current_status": status,
                    "current_depth": cell.get("depth", "none"),
                    "reason": self._suggest_reason(status, freshness),
                })

        # Sort: priority asc, business_value desc
        candidates.sort(key=lambda x: (x["priority"], -x["business_value"]))
        return candidates[:n]

    # ------------------------------------------------------------------
    # Disk scanning
    # ------------------------------------------------------------------

    def scan_existing_data(self):
        """Scan KB/ and data_cache/ to populate coverage from existing files.

        Checks:
        - KB/{vendor}-workflow/ directories for doc counts and char totals
        - data_cache/competitor_validation/feature_matrix/{vendor}.json for feature data
        """
        kb_root = PROJECT_ROOT / "KB"
        fm_root = PROJECT_ROOT / "data_cache" / "competitor_validation" / "feature_matrix"

        for vendor in VENDORS:
            # 1. Scan KB/{vendor}-workflow/
            kb_dir = kb_root / f"{vendor}-workflow"
            kb_doc_count = 0
            kb_total_chars = 0
            manifest_data = {}

            if kb_dir.is_dir():
                # Read manifest if exists
                manifest_path = kb_dir / "manifest.json"
                if manifest_path.exists():
                    try:
                        with manifest_path.open("r", encoding="utf-8") as fh:
                            manifest_data = json.load(fh)
                    except (json.JSONDecodeError, OSError):
                        pass

                # Count .md files and chars
                for md_file in sorted(kb_dir.glob("*.md")):
                    kb_doc_count += 1
                    try:
                        kb_total_chars += md_file.stat().st_size
                    except OSError:
                        pass

                # Prefer manifest totals if available
                if manifest_data:
                    kb_doc_count = manifest_data.get("document_count", kb_doc_count)
                    kb_total_chars = manifest_data.get("total_chars", kb_total_chars)

            # 2. Scan feature_matrix/{vendor}.json
            fm_path = fm_root / f"{vendor}.json"
            fm_data = {}
            if fm_path.exists():
                try:
                    with fm_path.open("r", encoding="utf-8") as fh:
                        fm_data = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    pass

            feature_matrix = fm_data.get("feature_matrix", {})
            explored_at = fm_data.get("explored_at", None)

            # 3. Populate cells
            for feature in FEATURES:
                fm_entry = feature_matrix.get(feature, {})
                has_fm = bool(fm_entry)

                if has_fm:
                    support_status = fm_entry.get("support_status", "none")
                    confidence = fm_entry.get("confidence", "low")
                    depth = STATUS_TO_DEPTH.get(support_status, "none")

                    # Determine status
                    if depth == "deep":
                        status = "explored"
                    elif depth in ("partial", "shallow"):
                        status = "partial"
                    else:
                        status = "unexplored"

                    self.update_cell(vendor, feature, {
                        "status": status,
                        "depth": depth,
                        "last_explored": explored_at,
                        "kb_doc_count": kb_doc_count,
                        "kb_total_chars": kb_total_chars,
                        "feature_matrix_exists": True,
                        "confidence": confidence,
                        "key_findings": fm_entry.get("key_findings", ""),
                        "explored_pages": fm_entry.get("explored_pages", []),
                        "quality_scores": {},
                    })
                elif kb_doc_count > 0:
                    # Has KB docs but no feature matrix entry for this feature
                    # Mark as partial with minimal info
                    self.update_cell(vendor, feature, {
                        "status": "partial",
                        "depth": "shallow",
                        "last_explored": explored_at,
                        "kb_doc_count": kb_doc_count,
                        "kb_total_chars": kb_total_chars,
                        "feature_matrix_exists": False,
                        "confidence": "low",
                        "quality_scores": {},
                    })
                # else: leave as unexplored (skeleton default)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_freshness(last_explored: str | None) -> str:
        """Compute freshness category from last_explored timestamp."""
        if not last_explored:
            return "none"
        try:
            last_dt = datetime.fromisoformat(str(last_explored))
            age = datetime.now() - last_dt
            if age.days <= 14:
                return "fresh"
            elif age.days <= 30:
                return "stale"
            else:
                return "expired"
        except (ValueError, TypeError):
            return "none"

    @staticmethod
    def _depth_icon(depth: str) -> str:
        if depth == "deep":
            return "\u25cf"  # ●
        elif depth in ("partial", "shallow"):
            return "\u25d1"  # ◑
        else:
            return "\u25cb"  # ○

    @staticmethod
    def _suggest_reason(status: str, freshness: str) -> str:
        if status == "unexplored":
            return "未探索"
        elif freshness in ("stale", "expired"):
            return f"数据过期({freshness})"
        elif status == "partial":
            return "仅浅层探索"
        return ""
