"""
ExplorationAssetRetriever: 从 KB 探索成果中一步提取分析所需的全部资产。

先查已有探索成果（KB文档+截图+原型+feature_matrix），
不够时输出探索指引。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

VENDORS = ["kingdee", "sap", "weaver", "seeyon"]
VENDOR_NAMES = {
    "kingdee": "金蝶云星空",
    "sap": "SAP SuccessFactors",
    "weaver": "泛微 e-cology",
    "seeyon": "致远 A8+",
}
FEATURE_NAMES = {
    "workflow_approval": "审批流程",
    "delegation": "代理人/委托",
    "form_design": "表单设计器",
    "branch_condition": "分支条件",
    "notification": "消息通知",
    "print_integration": "打印集成",
    "mobile_approval": "移动审批",
    "api_integration": "API/接口集成",
    "performance_monitor": "流程监控/效能",
    "signature": "电子签名",
}

# Mapping from feature_id to possible prototype directory names and screenshot keywords
_FEATURE_DIR_KEYWORDS: dict[str, list[str]] = {
    "workflow_approval": ["flow-designer", "workflow", "approval", "process-design"],
    "delegation": ["delegation", "委托", "代理"],
    "form_design": ["form", "表单"],
    "branch_condition": ["branch", "condition", "分支"],
    "notification": ["notification", "消息", "通知"],
    "print_integration": ["print", "打印"],
    "mobile_approval": ["mobile", "移动"],
    "api_integration": ["api", "接口"],
    "performance_monitor": ["monitor", "efficiency", "效率", "监控", "process-list"],
    "signature": ["signature", "签名", "签章"],
}

# Freshness thresholds (days)
_FRESH_DAYS = 30
_STALE_DAYS = 90


class ExplorationAssetRetriever:
    """Retrieve exploration assets (KB docs, screenshots, prototypes, feature matrix)
    for a vendor+feature cell, or batch-retrieve for a requirement across all vendors."""

    def __init__(self, project_root: str | Path | None = None):
        self.root = Path(project_root or Path.cwd())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(self, vendor_id: str, feature_id: str) -> dict[str, Any] | None:
        """Return all exploration assets for a vendor+feature cell.

        Checks:
        1. KB/{vendor}-workflow/ docs that match feature_id (via frontmatter feature_ids tag)
        2. data_cache/competitor_validation/feature_matrix/{vendor}.json
        3. conclusion/{vendor}-workflow/screenshots/ matching the feature
        4. conclusion/{vendor}-workflow/prototype/ matching the feature

        Returns None if no data exists for this cell.
        Returns dict with: kb_summary, support_status, key_differences,
                           screenshots, prototypes, feature_details,
                           explored_at, depth, confidence, freshness
        """
        kb_summary = self._read_kb_summary(vendor_id, feature_id)
        matrix_data = self._read_feature_matrix(vendor_id)
        screenshots = self._find_screenshots(vendor_id, feature_id)
        prototypes = self._find_prototypes(vendor_id, feature_id)

        # Extract feature-level details from the matrix
        feature_details: dict[str, Any] = {}
        if matrix_data:
            fm = matrix_data.get("feature_matrix", {})
            feature_details = fm.get(feature_id, {})

        # Determine if we have any meaningful data
        has_data = bool(kb_summary or feature_details or screenshots or prototypes)
        if not has_data:
            return None

        support_status = feature_details.get("support_status", "unknown")
        explored_at = feature_details.get("explored_at") or (matrix_data or {}).get("explored_at", "")
        confidence = feature_details.get("confidence", "low")
        freshness = self._check_freshness(vendor_id, feature_id)

        # Compute depth based on how much data we have
        depth_score = 0
        if kb_summary:
            depth_score += 1
        if feature_details:
            depth_score += 1
        if screenshots:
            depth_score += 1
        if prototypes:
            depth_score += 1
        depth = "deep" if depth_score >= 3 else ("medium" if depth_score >= 2 else "shallow")

        return {
            "vendor_id": vendor_id,
            "vendor_name": VENDOR_NAMES.get(vendor_id, vendor_id),
            "feature_id": feature_id,
            "feature_name": FEATURE_NAMES.get(feature_id, feature_id),
            "kb_summary": kb_summary or "",
            "support_status": support_status,
            "key_findings": feature_details.get("key_findings", ""),
            "explored_pages": feature_details.get("explored_pages", []),
            "screenshots": screenshots,
            "prototypes": prototypes,
            "feature_details": feature_details,
            "explored_at": explored_at,
            "depth": depth,
            "confidence": confidence,
            "freshness": freshness,
        }

    def retrieve_for_requirement(
        self,
        requirement_text: str,
        feature_ids: list[str],
    ) -> dict[str, Any]:
        """Batch retrieve for a requirement across all vendors.

        Returns:
        {
            "results": {
                "kingdee:workflow_approval": {...asset data...},
                ...
            },
            "gaps": [
                {"vendor": "sap", "feature": "workflow_approval", "reason": "未探索"},
                {"vendor": "weaver", "feature": "delegation", "reason": "数据过期(45天)"},
            ],
            "guidance": "以下竞品功能尚未探索...\n- SAP 审批流程：未探索。建议：..."
        }
        """
        results: dict[str, dict[str, Any]] = {}
        gaps: list[dict[str, str]] = []

        for vendor_id in VENDORS:
            for feature_id in feature_ids:
                cell_key = f"{vendor_id}:{feature_id}"
                data = self.retrieve(vendor_id, feature_id)
                if data is None:
                    gaps.append({
                        "vendor": vendor_id,
                        "vendor_name": VENDOR_NAMES.get(vendor_id, vendor_id),
                        "feature": feature_id,
                        "feature_name": FEATURE_NAMES.get(feature_id, feature_id),
                        "reason": "未探索",
                    })
                elif data.get("freshness") == "expired":
                    # Data exists but is too old
                    gaps.append({
                        "vendor": vendor_id,
                        "vendor_name": VENDOR_NAMES.get(vendor_id, vendor_id),
                        "feature": feature_id,
                        "feature_name": FEATURE_NAMES.get(feature_id, feature_id),
                        "reason": f"数据过期(探索于{data.get('explored_at', '未知')})",
                    })
                    results[cell_key] = data
                else:
                    results[cell_key] = data

        guidance = self.build_guidance(gaps) if gaps else ""

        return {
            "results": results,
            "gaps": gaps,
            "guidance": guidance,
        }

    # ------------------------------------------------------------------
    # Internal: KB doc reading
    # ------------------------------------------------------------------

    def _read_kb_summary(self, vendor_id: str, feature_id: str) -> str | None:
        """Read KB doc matching feature_id and extract summary."""
        kb_dir = self.root / "KB" / f"{vendor_id}-workflow"
        if not kb_dir.is_dir():
            return None

        for md_file in sorted(kb_dir.glob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # Parse YAML frontmatter
            frontmatter, body = self._parse_frontmatter(text)
            if not frontmatter:
                continue

            doc_features = frontmatter.get("feature_ids", [])
            if isinstance(doc_features, str):
                doc_features = [doc_features]

            if feature_id in doc_features:
                # Return the first 500 chars of body content
                clean_body = body.strip()
                if clean_body:
                    return clean_body[:500]

        return None

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        """Parse YAML frontmatter delimited by --- from markdown text.

        Returns (frontmatter_dict, body_text). If no frontmatter, returns ({}, full_text).
        """
        # Frontmatter may start at line 0 (with or without BOM)
        stripped = text.lstrip("\ufeff")
        if not stripped.startswith("---"):
            return {}, text

        parts = stripped.split("---", 2)
        if len(parts) < 3:
            return {}, text

        try:
            fm = yaml.safe_load(parts[1])
        except Exception:
            return {}, text

        if not isinstance(fm, dict):
            return {}, text

        return fm, parts[2]

    # ------------------------------------------------------------------
    # Internal: feature matrix
    # ------------------------------------------------------------------

    def _read_feature_matrix(self, vendor_id: str) -> dict[str, Any] | None:
        """Read feature_matrix JSON for a vendor."""
        path = self.root / "data_cache" / "competitor_validation" / "feature_matrix" / f"{vendor_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Internal: screenshots
    # ------------------------------------------------------------------

    def _find_screenshots(self, vendor_id: str, feature_id: str) -> list[dict[str, Any]]:
        """Find screenshots related to a feature."""
        screenshots_dir = self.root / "conclusion" / f"{vendor_id}-workflow" / "screenshots"
        if not screenshots_dir.is_dir():
            return []

        keywords = _FEATURE_DIR_KEYWORDS.get(feature_id, [feature_id])
        results: list[dict[str, Any]] = []

        for img_path in sorted(screenshots_dir.glob("*.png")):
            name_lower = img_path.name.lower()
            # Match by filename containing feature-related keywords
            if any(kw.lower() in name_lower for kw in keywords):
                results.append({
                    "path": str(img_path.relative_to(self.root)),
                    "filename": img_path.name,
                })

        # If no keyword match, try to match from KB manifest document mapping
        # For now, return what we found (may be empty)
        return results

    # ------------------------------------------------------------------
    # Internal: prototypes
    # ------------------------------------------------------------------

    def _find_prototypes(self, vendor_id: str, feature_id: str) -> list[dict[str, Any]]:
        """Find prototype HTML pages related to a feature."""
        proto_dir = self.root / "conclusion" / f"{vendor_id}-workflow" / "prototype"
        if not proto_dir.is_dir():
            return []

        keywords = _FEATURE_DIR_KEYWORDS.get(feature_id, [feature_id])
        results: list[dict[str, Any]] = []

        for sub in sorted(proto_dir.iterdir()):
            if not sub.is_dir():
                continue
            name_lower = sub.name.lower()
            if any(kw.lower() in name_lower for kw in keywords):
                index_html = sub / "index.html"
                if index_html.exists():
                    results.append({
                        "path": str(sub.relative_to(self.root)),
                        "name": sub.name,
                        "has_html": True,
                        "has_css": (sub / "page.css").exists(),
                        "has_js": (sub / "page.js").exists(),
                    })

        return results

    # ------------------------------------------------------------------
    # Internal: freshness check
    # ------------------------------------------------------------------

    def _check_freshness(self, vendor_id: str, feature_id: str) -> str:
        """Check data freshness. Returns: fresh/stale/expired/none"""
        matrix = self._read_feature_matrix(vendor_id)
        if not matrix:
            # Try KB frontmatter date
            kb_dir = self.root / "KB" / f"{vendor_id}-workflow"
            if not kb_dir.is_dir():
                return "none"
            for md_file in sorted(kb_dir.glob("*.md")):
                try:
                    text = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue
                fm, _ = self._parse_frontmatter(text)
                doc_features = fm.get("feature_ids", [])
                if isinstance(doc_features, str):
                    doc_features = [doc_features]
                if feature_id in doc_features:
                    explored_at = fm.get("explored_at", "")
                    return self._date_to_freshness(str(explored_at))
            return "none"

        explored_at = matrix.get("explored_at", "")
        return self._date_to_freshness(str(explored_at))

    def _date_to_freshness(self, date_str: str) -> str:
        """Convert an ISO date string to a freshness label."""
        if not date_str:
            return "none"
        try:
            explored = datetime.fromisoformat(date_str).date() if "T" in date_str else datetime.strptime(date_str, "%Y-%m-%d").date()
            age_days = (datetime.now().date() - explored).days
        except Exception:
            return "none"

        if age_days <= _FRESH_DAYS:
            return "fresh"
        elif age_days <= _STALE_DAYS:
            return "stale"
        else:
            return "expired"

    # ------------------------------------------------------------------
    # Guidance builder
    # ------------------------------------------------------------------

    def build_guidance(self, gaps: list[dict[str, Any]]) -> str:
        """Build user-friendly guidance for exploration gaps."""
        if not gaps:
            return ""

        lines = ["以下竞品功能尚未充分探索，建议安排专项探索：", ""]

        for gap in gaps:
            vendor_name = gap.get("vendor_name", gap.get("vendor", ""))
            feature_name = gap.get("feature_name", gap.get("feature", ""))
            reason = gap.get("reason", "未探索")
            vendor_id = gap.get("vendor", "")

            suggestion = f"使用 product-exploration-v2 skill 探索 {vendor_name} 的{feature_name}模块"
            lines.append(f"- **{vendor_name} · {feature_name}**：{reason}。建议：{suggestion}")

        lines.append("")
        lines.append("探索完成后，数据将自动存入 KB 和 feature_matrix，下次分析可直接复用。")
        return "\n".join(lines)
