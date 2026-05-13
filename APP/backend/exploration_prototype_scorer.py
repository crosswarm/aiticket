"""
PrototypeQualityScorer: 原型保真度评分器（7维度）

纯 Python 文件读取 + 正则，无 DOM 解析器/LLM 调用，秒级返回。
供 Darwin 进化框架 reqpool 适配器调用，评估 HTML 原型与调研报告的一致性。
"""
from __future__ import annotations

import os
import re
from pathlib import Path


class PrototypeQualityScorer:
    """7-dimension prototype fidelity scoring."""

    # 企业级 CSS 类名关键词
    ENTERPRISE_CLASS_PATTERNS = [
        r"prop-field", r"prop-section", r"toolbar-btn",
        r"app-header", r"breadcrumb", r"properties-panel",
        r"node-toolbox", r"canvas-", r"designer-",
        r"data-table", r"list-view", r"form-group",
    ]

    def score_prototype(
        self,
        prototype_dir: str,
        findings_path: str,
        screenshots_dir: str,
    ) -> dict:
        """Return a dict of 7 fidelity dimensions plus raw counts.

        Dimensions:
          layout_accuracy          — does HTML DOM match the layout described in findings
          element_completeness     — fraction of buttons from findings present in HTML
          data_accuracy            — fraction of real data names from findings in HTML
          interaction_coverage     — JS event listener count / interactive HTML element count
          style_conformance        — CSS uses --primary var + enterprise-style class names
          code_quality             — no external CDN refs + no undefined JS refs
          standalone_runnable      — all referenced CSS/JS files exist in prototype dir
        """
        proto_dir = Path(prototype_dir)
        html_text = _read_file(proto_dir / "index.html")
        css_text = _read_file(proto_dir / "page.css")
        js_text = _read_file(proto_dir / "page.js")
        findings_text = _read_file(findings_path)

        all_code = html_text + css_text + js_text

        # 1. Layout accuracy
        layout_accuracy = _score_layout_accuracy(findings_text, html_text)

        # 2. Element completeness
        buttons_in_findings = _extract_buttons_from_findings(findings_text)
        buttons_in_html = _extract_buttons_from_html(html_text, js_text)
        elem_completeness = _calc_coverage(buttons_in_findings, buttons_in_html)

        # 3. Data accuracy
        real_names = _extract_real_data_names(findings_text)
        names_in_html = set()
        for name in real_names:
            if name in html_text or name in js_text:
                names_in_html.add(name)
        data_accuracy = len(names_in_html) / max(len(real_names), 1)

        # 4. Interaction coverage
        event_listeners = _count_event_listeners(js_text)
        interactive_elements = _count_interactive_elements(html_text)
        interaction_coverage = event_listeners / max(interactive_elements, 1)

        # 5. Style conformance
        style_conformance = _score_style_conformance(css_text, html_text, self.ENTERPRISE_CLASS_PATTERNS)

        # 6. Code quality
        code_quality = _score_code_quality(html_text, css_text, js_text)

        # 7. Standalone runnable
        standalone = _score_standalone(proto_dir, html_text)

        return {
            "layout_accuracy": round(layout_accuracy, 4),
            "element_completeness": round(elem_completeness, 4),
            "data_accuracy": round(data_accuracy, 4),
            "interaction_coverage": round(min(interaction_coverage, 1.0), 4),
            "style_conformance": round(style_conformance, 4),
            "code_quality": round(code_quality, 4),
            "standalone_runnable": round(standalone, 4),
            # raw counts
            "raw_buttons_in_findings": len(buttons_in_findings),
            "raw_buttons_in_html": len(buttons_in_html),
            "raw_real_names": len(real_names),
            "raw_names_in_html": len(names_in_html),
            "raw_event_listeners": event_listeners,
            "raw_interactive_elements": interactive_elements,
        }


# ── 内部辅助函数 ──────────────────────────────────────────────────────────────


def _read_file(path) -> str:
    """Read file as UTF-8 text, return empty string if missing."""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def _score_layout_accuracy(findings: str, html: str) -> float:
    """Check if the HTML matches the layout pattern described in findings.

    Looks for layout descriptors like '三栏布局', '左树右表', '纯列表' in findings,
    then checks HTML for corresponding DOM structures.
    """
    score = 0.0
    checks = 0

    # Three-column layout
    if "三栏布局" in findings or "三栏" in findings:
        checks += 1
        # Expect aside + main + aside or similar 3-region pattern
        aside_count = len(re.findall(r"<aside", html))
        has_main = bool(re.search(r'class="[^"]*(?:canvas|main|center|content)', html))
        if aside_count >= 2 or (aside_count >= 1 and has_main):
            score += 1.0

    # Left-tree-right-table
    if "左树右表" in findings:
        checks += 1
        has_tree = bool(re.search(r'class="[^"]*(?:tree|sidebar|nav-tree)', html))
        has_table = bool(re.search(r"<table|class=\"[^\"]*(?:data-table|list-view)", html))
        if has_tree and has_table:
            score += 1.0
        elif has_tree or has_table:
            score += 0.5

    # Pure list
    if "纯列表" in findings:
        checks += 1
        has_table = bool(re.search(r"<table", html))
        if has_table:
            score += 1.0

    # Chart + table
    if "图表" in findings and "表格" in findings:
        checks += 1
        has_chart = bool(re.search(r'class="[^"]*chart', html))
        has_table = bool(re.search(r"<table", html))
        if has_chart and has_table:
            score += 1.0
        elif has_chart or has_table:
            score += 0.5

    # Toolbar
    if "工具栏" in findings:
        checks += 1
        has_toolbar = bool(re.search(r'class="[^"]*toolbar', html))
        if has_toolbar:
            score += 1.0

    # Properties/settings panel
    if "属性面板" in findings or "设置面板" in findings:
        checks += 1
        has_panel = bool(re.search(r'class="[^"]*(?:properties|panel|settings)', html))
        if has_panel:
            score += 1.0

    if checks == 0:
        return 0.5  # neutral if no layout descriptors found
    return score / checks


def _extract_buttons_from_findings(text: str) -> set[str]:
    """Extract button labels from findings markdown tables and lists."""
    buttons: set[str] = set()
    # Table rows: | 保存 | ... | or | 按钮 | 说明 | pattern
    for m in re.finditer(r"\|\s*([\u4e00-\u9fff][\u4e00-\u9fff\w]{0,8})\s*\|", text):
        name = m.group(1).strip()
        if len(name) >= 2 and name not in (
            "按钮", "说明", "功能", "项目", "描述", "类型", "用途", "字段名",
            "颜色", "图标", "颜色区块", "节点类型", "控制项", "编号", "文件名",
            "目标", "选择器", "来源",
        ):
            buttons.add(name)
    return buttons


def _extract_buttons_from_html(html: str, js: str) -> set[str]:
    """Extract button texts from HTML and JS."""
    buttons: set[str] = set()
    # <button ...>文本</button>
    for m in re.finditer(r"<button[^>]*>([\u4e00-\u9fff][\u4e00-\u9fff\w/]*)</button>", html):
        buttons.add(m.group(1).strip())
    # button text in JS strings
    for m in re.finditer(r"label:\s*['\"]([^'\"]+)['\"]", js):
        buttons.add(m.group(1).strip())
    # data-action attribute values mapped to Chinese text in JS
    for m in re.finditer(r"['\"]([^'\"]*[\u4e00-\u9fff]+[^'\"]*)['\"]", js):
        val = m.group(1).strip()
        if 2 <= len(val) <= 10:
            buttons.add(val)
    return buttons


def _extract_real_data_names(text: str) -> set[str]:
    """Extract real data names from findings (流程编码, 流程名称, etc.)."""
    names: set[str] = set()
    # Look for specific data patterns: 流程编码, 流程名称, 项目任务书, etc.
    patterns = [
        r"流程编码", r"流程名称", r"流程标识", r"流程描述",
        r"节点名称", r"节点编码", r"项目任务书", r"集团公司",
        r"委托人", r"受托人", r"委托方式", r"委托期间",
        r"单据", r"所属组织", r"流程类型",
    ]
    for pat in patterns:
        if re.search(pat, text):
            names.add(pat)
    # Also extract data from table cells in the data/example columns
    for m in re.finditer(r"示例值?\s*\|\s*\n\|[^|]+\|[^|]+\|[^|]+\|\s*([\u4e00-\u9fff][\w\u4e00-\u9fff_]+)", text):
        names.add(m.group(1).strip())
    return names


def _calc_coverage(expected: set[str], actual: set[str]) -> float:
    """Calculate how many expected items appear in actual."""
    if not expected:
        return 1.0
    found = sum(1 for e in expected if any(e in a or a in e for a in actual))
    return found / len(expected)


def _count_event_listeners(js: str) -> int:
    """Count addEventListener calls and event handler assignments in JS."""
    add_listener = len(re.findall(r"addEventListener\s*\(", js))
    on_handler = len(re.findall(r"\.\s*on(?:click|change|input|submit|drag\w*|drop|key\w*)\s*=", js))
    return add_listener + on_handler


def _count_interactive_elements(html: str) -> int:
    """Count interactive HTML elements (buttons, inputs, selects, etc.)."""
    buttons = len(re.findall(r"<button", html))
    inputs = len(re.findall(r"<input", html))
    selects = len(re.findall(r"<select", html))
    textareas = len(re.findall(r"<textarea", html))
    draggable = len(re.findall(r'draggable="true"', html))
    return buttons + inputs + selects + textareas + draggable


def _score_style_conformance(css: str, html: str, patterns: list[str]) -> float:
    """Check CSS uses --primary variable and has enterprise-style class names."""
    checks = 0
    score = 0.0

    # Check --primary CSS variable
    checks += 1
    if "--primary" in css:
        score += 1.0

    # Check enterprise class patterns
    matched_patterns = sum(1 for p in patterns if re.search(p, css + html))
    checks += 1
    score += min(matched_patterns / max(len(patterns) // 2, 1), 1.0)

    # Check font-family includes standard Chinese fonts
    checks += 1
    if "Microsoft YaHei" in css or "PingFang" in css:
        score += 1.0

    # Check CSS custom properties usage
    checks += 1
    var_usages = len(re.findall(r"var\(--", css))
    score += min(var_usages / 10, 1.0)

    return score / max(checks, 1)


def _score_code_quality(html: str, css: str, js: str) -> float:
    """Check for code quality issues."""
    checks = 0
    score = 0.0
    all_code = html + css + js

    # No external CDN references
    checks += 1
    cdn_refs = len(re.findall(r"https?://(?:cdn|unpkg|cdnjs|jsdelivr)", all_code))
    if cdn_refs == 0:
        score += 1.0

    # No console.error / undefined references
    checks += 1
    undefined_refs = len(re.findall(r"\bundefined\b", js))
    if undefined_refs == 0:
        score += 1.0

    # Uses strict mode or modern patterns
    checks += 1
    if "const " in js or "let " in js:
        score += 1.0

    # No inline styles in HTML (prefer CSS classes)
    checks += 1
    inline_styles = len(re.findall(r'style="[^"]{20,}"', html))
    if inline_styles <= 2:
        score += 1.0
    elif inline_styles <= 5:
        score += 0.5

    return score / max(checks, 1)


def _score_standalone(proto_dir: Path, html: str) -> float:
    """Verify all referenced CSS/JS files exist in the prototype directory."""
    # Extract href and src references
    refs: list[str] = []
    for m in re.finditer(r'(?:href|src)="\.?/?([^"]+)"', html):
        ref = m.group(1)
        if not ref.startswith("http") and not ref.startswith("data:"):
            refs.append(ref)

    if not refs:
        return 1.0

    found = 0
    for ref in refs:
        # Strip leading ./
        ref_clean = ref.lstrip("./")
        if (proto_dir / ref_clean).exists():
            found += 1

    return found / len(refs)
