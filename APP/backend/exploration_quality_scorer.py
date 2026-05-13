"""
ReportQualityScorer: 探索报告结构质量评分器（7维度）

纯 Python 文件读取 + 正则，无 LLM 调用，秒级返回。
供 Darwin 进化框架 reqpool 适配器调用，评估探索报告的完整性与深度。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path


class ReportQualityScorer:
    """7-dimension structural quality scoring for exploration reports."""

    # 深度标志关键词：表明某个章节做了字段级/按钮级详细记录
    DEPTH_KEYWORDS = [
        "字段：", "字段名", "工具栏按钮：", "工具栏按钮",
        "列头", "列表列头", "按钮 |", "| 按钮", "| 字段",
        "配置项", "Tab ", "控制项",
    ]

    def score_report(
        self,
        findings_path: str,
        kb_dir: str,
        screenshots_dir: str,
    ) -> dict:
        """Return a dict of 7 quality dimensions plus raw counts.

        Dimensions:
          completeness        — ### sections in findings / menu tree nodes
          depth_ratio         — sections with depth keywords / total sections
          screenshot_coverage — referenced screenshots / total screenshot files
          field_density       — "- " list items under field sections / page count
          cross_ref_accuracy  — field/button names from findings found in KB docs
          structure_conformance — fraction of KB docs with frontmatter + standard sections
          data_freshness      — fraction of KB docs with explored_at within 30 days
        """
        findings_text = _read_text(findings_path)
        kb_docs = _read_kb_docs(kb_dir)
        screenshot_files = _list_files(screenshots_dir, exts=(".png", ".jpg", ".jpeg", ".webp"))

        sections = _extract_sections(findings_text)
        menu_nodes = _count_menu_tree_nodes(findings_text)
        depth_sections = _count_depth_sections(findings_text, sections, self.DEPTH_KEYWORDS)
        screenshot_refs = _extract_screenshot_refs(findings_text)
        field_items = _count_field_list_items(findings_text)
        page_count = max(_count_pages(findings_text), 1)

        # cross-ref: extract button/field names from findings, check KB
        names_in_findings = _extract_entity_names(findings_text)
        kb_full_text = " ".join(doc["text"] for doc in kb_docs)
        cross_ref_hits = sum(1 for n in names_in_findings if n in kb_full_text) if names_in_findings else 0

        # structure conformance
        conforming = sum(1 for doc in kb_docs if _has_frontmatter(doc["text"]) and _has_standard_sections(doc["text"]))

        # data freshness
        fresh = sum(1 for doc in kb_docs if _is_fresh(doc["text"], days=30))

        total_sections = len(sections)
        total_kb = max(len(kb_docs), 1)
        total_screenshots = max(len(screenshot_files), 1)
        total_refs = len(screenshot_refs)
        total_names = max(len(names_in_findings), 1)

        return {
            "completeness": round(total_sections / max(menu_nodes, 1), 4),
            "depth_ratio": round(depth_sections / max(total_sections, 1), 4),
            "screenshot_coverage": round(total_refs / total_screenshots, 4),
            "field_density": round(field_items / page_count, 4),
            "cross_ref_accuracy": round(cross_ref_hits / total_names, 4),
            "structure_conformance": round(conforming / total_kb, 4),
            "data_freshness": round(fresh / total_kb, 4),
            # raw counts for debugging
            "raw_sections": total_sections,
            "raw_menu_nodes": menu_nodes,
            "raw_depth_sections": depth_sections,
            "raw_screenshot_refs": total_refs,
            "raw_screenshot_files": len(screenshot_files),
            "raw_field_items": field_items,
            "raw_page_count": page_count,
            "raw_cross_ref_hits": cross_ref_hits,
            "raw_entity_names": len(names_in_findings),
            "raw_kb_docs": len(kb_docs),
            "raw_conforming_docs": conforming,
            "raw_fresh_docs": fresh,
        }


# ── 内部辅助函数 ──────────────────────────────────────────────────────────────


def _read_text(path: str) -> str:
    """Read file as UTF-8 text, return empty string if missing."""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def _read_kb_docs(kb_dir: str) -> list[dict]:
    """Read all .md files under kb_dir, return list of {path, text}."""
    p = Path(kb_dir)
    if not p.is_dir():
        return []
    docs = []
    for md in sorted(p.glob("*.md")):
        docs.append({"path": str(md), "text": md.read_text(encoding="utf-8", errors="replace")})
    return docs


def _list_files(directory: str, exts: tuple[str, ...]) -> list[str]:
    """List files in directory matching extensions."""
    p = Path(directory)
    if not p.is_dir():
        return []
    return [f.name for f in p.iterdir() if f.is_file() and f.suffix.lower() in exts]


def _extract_sections(text: str) -> list[str]:
    """Extract all ### level headings from markdown."""
    return re.findall(r"^###\s+(.+)$", text, re.MULTILINE)


def _count_menu_tree_nodes(text: str) -> int:
    """Count menu tree nodes in the findings.

    Heuristic: lines starting with │, ├, └, or indented ├/└ in a tree block,
    plus lines with ⏳/🔍/📋 markers.
    """
    tree_lines = re.findall(r"^[\s│]*[├└]──\s+.+", text, re.MULTILINE)
    marker_lines = re.findall(r"[⏳🔍📋]", text)
    # Use the larger of tree-line count or marker count as the node estimate
    return max(len(tree_lines), len(marker_lines) // 1) if tree_lines or marker_lines else 1


def _count_depth_sections(text: str, sections: list[str], keywords: list[str]) -> int:
    """Count sections whose body contains depth-indicator keywords."""
    # Split text by ### headings and check each body
    parts = re.split(r"^###\s+.+$", text, flags=re.MULTILINE)
    if len(parts) <= 1:
        return 0
    # parts[0] is before first ###, parts[1..] correspond to sections
    bodies = parts[1:]
    count = 0
    for body in bodies:
        if any(kw in body for kw in keywords):
            count += 1
    return count


def _extract_screenshot_refs(text: str) -> list[str]:
    """Extract screenshot filename references from findings text."""
    # Match patterns like: a-028-process-design-clean.png, b-010-process-designer.png
    refs = re.findall(r"[a-z]-\d{3}-[\w-]+\.png", text)
    # Also match kd-* pattern
    refs += re.findall(r"kd-\d{3}-[\w-]+\.png", text)
    return list(set(refs))


def _count_field_list_items(text: str) -> int:
    """Count markdown list items (lines starting with '- ') in field-related sections."""
    # Count all "- " list items as a proxy for field/button density
    return len(re.findall(r"^[\s]*-\s+.+", text, re.MULTILINE))


def _count_pages(text: str) -> int:
    """Count explored pages (lines with 🔍深探 or 📋概览)."""
    deep = len(re.findall(r"🔍深探", text))
    overview = len(re.findall(r"📋概览", text))
    return deep + overview


def _extract_entity_names(text: str) -> list[str]:
    """Extract button and field names from findings text.

    Looks for Chinese names in table rows (| 按钮名 | ...) and
    field names after labels.
    """
    names: set[str] = set()
    # Table row pattern: | 中文名 | ... |
    for m in re.finditer(r"\|\s*([\u4e00-\u9fff][\u4e00-\u9fff\w/（）]{1,15})\s*\|", text):
        name = m.group(1).strip()
        if len(name) >= 2 and name not in ("按钮", "说明", "功能", "项目", "描述", "类型", "用途", "字段名"):
            names.add(name)
    return list(names)


def _has_frontmatter(text: str) -> bool:
    """Check if document starts with YAML frontmatter (---)."""
    return text.lstrip().startswith("---")


def _has_standard_sections(text: str) -> bool:
    """Check if document has at least 2 ## level headings."""
    headings = re.findall(r"^##\s+.+$", text, re.MULTILINE)
    return len(headings) >= 2


def _is_fresh(text: str, days: int = 30) -> bool:
    """Check if explored_at in frontmatter is within `days` days of today."""
    m = re.search(r"explored_at:\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        return False
    try:
        explored = datetime.strptime(m.group(1), "%Y-%m-%d")
        return (datetime.now() - explored) <= timedelta(days=days)
    except ValueError:
        return False
