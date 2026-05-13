from __future__ import annotations

import re
from typing import Any


class CompetitorQualityGuardian:
    SCORE_THRESHOLDS = {
        "richness": 3,
        "reliability": 4,
        "value": 3,
        "relevance": 4,
    }

    def __init__(self) -> None:
        self._negative_terms = [
            "supply chain",
            "inventory",
            "transport",
            "procurement",
            "forecast",
            "数字化办公",
            "合同管理",
            "人事管理",
            "项目管理",
            "知识管理",
            "报表中心",
            "滚动查看更多",
            "all enterprise digital",
            "全面覆盖",
            "首页",
            "homepage",
        ]
        self._marketing_terms = [
            "数字化办公",
            "解决方案覆盖",
            "全场景产品介绍",
            "滚动查看更多",
            "官网首页",
            "产品矩阵",
            "all-in-one",
            "领先",
            "全面覆盖",
        ]
        self._feature_terms = [
            "workflow",
            "approval",
            "approver",
            "substitute",
            "substitution",
            "delegate",
            "delegation",
            "代理",
            "委托",
            "审批",
            "审批人",
            "流程",
            "待办",
            "future approver",
            "未来审批",
        ]

    def evaluate_bundle(self, bundle: dict[str, Any], requirement: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        requirement = requirement or {}
        filtered_bundle = {
            **(bundle or {}),
            "scope": self._normalize_scope((bundle or {}).get("scope") or {}, requirement),
            "vendors": [],
        }

        issues: list[str] = []
        kept_vendor_count = 0
        kept_citation_count = 0
        trusted_kept_count = 0
        signal_count = 0
        original_citation_count = 0

        rejected_citation_count = 0

        for vendor in (bundle or {}).get("vendors", []) or []:
            original_citation_count += len(vendor.get("citations", []) or [])
            filtered_vendor, vendor_stats = self._evaluate_vendor(vendor, filtered_bundle["scope"], requirement)
            filtered_bundle["vendors"].append(filtered_vendor)
            kept_vendor_count += 1 if filtered_vendor.get("feature_match") else 0
            kept_citation_count += vendor_stats["kept_citations"]
            trusted_kept_count += vendor_stats["trusted_kept_citations"]
            signal_count += vendor_stats["signal_count"]
            rejected_citation_count += vendor_stats["rejected_citations"]
            if vendor_stats["kept_citations"] == 0:
                issues.append(f"{filtered_vendor.get('vendor', '竞品')} 当前没有通过质量门禁的高相关证据。")

        filtered_bundle["scope"]["selected_vendors"] = filtered_bundle["scope"].get("selected_vendors") or [
            item.get("vendor", "") for item in filtered_bundle["vendors"] if item.get("vendor")
        ]
        filtered_bundle["summary"] = self._build_bundle_summary(filtered_bundle)
        filtered_bundle["gaps"] = self._build_bundle_gaps(filtered_bundle)
        filtered_bundle["next_actions"] = self._build_next_actions(filtered_bundle)

        vendor_total = max(len(filtered_bundle["vendors"]), 1)
        richness = 1
        if kept_citation_count >= vendor_total * 2:
            richness = 5
        elif kept_citation_count >= vendor_total and signal_count >= vendor_total * 2:
            richness = 4
        elif kept_citation_count >= vendor_total:
            richness = 3
        elif kept_citation_count > 0:
            richness = 2

        reliability = 1
        if trusted_kept_count >= vendor_total and kept_citation_count >= vendor_total:
            reliability = 4
        if trusted_kept_count >= vendor_total and kept_citation_count >= vendor_total * 2:
            reliability = 5
        elif kept_citation_count > 0 and trusted_kept_count == 0:
            reliability = 3
        elif kept_citation_count == 0:
            reliability = 2 if signal_count > 0 else 1

        value = 1
        if signal_count >= vendor_total * 3 and kept_citation_count >= vendor_total:
            value = 5
        elif signal_count >= vendor_total * 2 and kept_citation_count >= vendor_total:
            value = 4
        elif kept_citation_count >= vendor_total:
            value = 3
        elif kept_citation_count > 0:
            value = 2

        relevance = 1
        if kept_vendor_count == vendor_total and kept_citation_count >= vendor_total:
            relevance = 5
        elif kept_vendor_count >= 1 and kept_citation_count >= 1:
            relevance = 4
        elif kept_citation_count > 0:
            relevance = 3
        elif signal_count > 0:
            relevance = 2

        blocking_issues = []
        if relevance < self.SCORE_THRESHOLDS["relevance"]:
            blocking_issues.append("竞品证据与当前需求点的相关度不足，仍混入无关模块或营销内容。")
        if reliability < self.SCORE_THRESHOLDS["reliability"]:
            blocking_issues.append("竞品结论缺少足够可靠的官方证据或真实验证支撑。")
        if value < self.SCORE_THRESHOLDS["value"]:
            blocking_issues.append("竞品内容价值不足，无法直接支撑方案借鉴或风险判断。")
        if richness < self.SCORE_THRESHOLDS["richness"]:
            blocking_issues.append("竞品内容丰富度不足，缺少位置、限制或可借鉴模式。")

        blocking_issues.extend(self._dedupe(issues))
        insufficiency_only = (
            rejected_citation_count == 0
            and bool(blocking_issues)
            and (
                kept_citation_count == 0
                or kept_citation_count < vendor_total
                or trusted_kept_count < max(1, kept_vendor_count)
            )
        )
        if not blocking_issues:
            decision = "passed"
        elif kept_citation_count == 0 and original_citation_count == 0:
            decision = "warning"
        elif insufficiency_only:
            decision = "warning"
        else:
            decision = "failed"
        assessment = {
            "target_type": "competitor_bundle",
            "decision": decision,
            "scores": {
                "richness": richness,
                "reliability": reliability,
                "value": value,
                "relevance": relevance,
            },
            "blocking_issues": self._dedupe(blocking_issues),
            "improvement_actions": self._build_improvement_actions(filtered_bundle),
            "item_judgements": [
                {
                    "vendor": vendor.get("vendor", ""),
                    "kept": len(vendor.get("citations", []) or []),
                    "dropped": len([item for item in vendor.get("item_judgements", []) if item.get("judgement") == "drop"]),
                }
                for vendor in filtered_bundle["vendors"]
            ],
            "summary": filtered_bundle["summary"],
        }
        return filtered_bundle, assessment

    def evaluate_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        artifact = artifact or {}
        existing = artifact.get("quality_assessment", {}) or {}
        existing_scores = existing.get("scores", {}) or {}
        draft_content = str(artifact.get("draft_content") or "")
        competitor_bundle = artifact.get("competitor_comparison", {}) or {}

        scores = {
            "richness": int(existing_scores.get("richness", 3) or 3),
            "reliability": int(existing_scores.get("reliability", 3) or 3),
            "value": int(existing_scores.get("value", 3) or 3),
            "relevance": int(existing_scores.get("relevance", 3) or 3),
        }
        issues: list[str] = []

        hard_fail = False

        if any(term in draft_content for term in self._marketing_terms):
            scores["relevance"] = min(scores["relevance"], 2)
            scores["value"] = min(scores["value"], 2)
            issues.append("竞品段落仍带有明显营销/官网导购文案，和当前需求点无关。")
            hard_fail = True

        if any(term in draft_content.lower() for term in ["supply chain", "inventory", "transport", "procurement"]):
            scores["relevance"] = min(scores["relevance"], 2)
            issues.append("竞品段落混入了供应链等无关模块内容。")
            hard_fail = True

        verified_count = 0
        citation_count = 0
        evidence_status = str(competitor_bundle.get("evidence_status") or "")
        for vendor in (competitor_bundle.get("vendors") or []):
            citation_count += len(vendor.get("citations", []) or [])
            verification = vendor.get("verification", {}) or {}
            if verification.get("status") in {"verified_public", "verified_authenticated"}:
                verified_count += 1

        if citation_count == 0:
            scores["reliability"] = min(scores["reliability"], 2)
            issues.append("竞品结论缺少可追溯证据链接。")
        elif verified_count > 0:
            scores["reliability"] = min(max(scores["reliability"], 4), 5)

        if scores["relevance"] < self.SCORE_THRESHOLDS["relevance"]:
            issues.append("竞品结论未通过相关度门禁。")
        if scores["reliability"] < self.SCORE_THRESHOLDS["reliability"]:
            issues.append("竞品结论未通过真实可靠性门禁。")
        if scores["value"] < self.SCORE_THRESHOLDS["value"]:
            issues.append("竞品结论未通过内容价值门禁。")
        if scores["richness"] < self.SCORE_THRESHOLDS["richness"]:
            issues.append("竞品结论未通过内容丰富度门禁。")

        decision = "passed"
        if issues:
            decision = "failed" if hard_fail or evidence_status == "invalid" else "warning"

        return {
            "target_type": "draft_content",
            "decision": decision,
            "scores": scores,
            "blocking_issues": self._dedupe(issues),
            "improvement_actions": [
                "仅保留和需求点直接相关的竞品能力、页面位置、限制和证据链接。",
                "补充公开截图或登录后截图，避免把未验证内容写成已确认事实。",
            ],
            "summary": (
                "竞品内容已通过质量门禁。"
                if not issues
                else ("竞品内容存在明显错误或无关信息，需先纠偏。" if decision == "failed" else "竞品证据不足，已降级为告警。")
            ),
        }

    def _evaluate_vendor(
        self,
        vendor: dict[str, Any],
        scope: dict[str, Any],
        requirement: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        item_judgements = []
        kept_citations = []
        signal_count = 0
        trusted_kept_citations = 0
        verification = vendor.get("verification", {}) or {}
        verification_status = verification.get("status", "")

        for citation in vendor.get("citations", []) or []:
            source_type = self._infer_source_type(citation)
            judgement, reason = self._judge_citation(citation, scope, requirement)
            item_judgements.append(
                {
                    "title": citation.get("title", "") or citation.get("url", ""),
                    "url": citation.get("url", ""),
                    "judgement": judgement,
                    "reason": reason,
                    "source_type": source_type,
                }
            )
            if judgement == "keep":
                kept_citations.append({**citation, "source_type": source_type})
                signal_count += 2
                if source_type == "official" or (
                    source_type == "internal_dossier"
                    and verification_status in {"verified_public", "verified_authenticated"}
                ):
                    trusted_kept_citations += 1

        borrowable_patterns = [
            item for item in (vendor.get("borrowable_patterns") or [])
            if not self._contains_negative_noise(item)
        ]
        if borrowable_patterns:
            signal_count += 1

        risks_or_limits = [
            item for item in (vendor.get("risks_or_limits") or [])
            if item and not self._contains_negative_noise(item)
        ]
        if risks_or_limits:
            signal_count += 1

        summary = str(vendor.get("implementation_summary") or "")
        if summary and not self._contains_negative_noise(summary):
            signal_count += 1

        filtered_vendor = {
            **vendor,
            "implementation_summary": summary,
            "citations": kept_citations[:3],
            "borrowable_patterns": borrowable_patterns[:4],
            "risks_or_limits": risks_or_limits[:4],
            "item_judgements": item_judgements,
            "feature_match": bool(kept_citations),
            "match_reason": "命中了和当前需求点直接相关的官方/高可信证据。" if kept_citations else "未命中足够高相关证据。",
            "evidence_items": [
                {
                    "title": citation.get("title", ""),
                    "url": citation.get("url", ""),
                    "source_level": citation.get("source_type", "secondary"),
                    "snippet": citation.get("snippet", ""),
                }
                for citation in kept_citations[:3]
            ],
            "verification": vendor.get("verification") or {"status": "unverified", "captures": [], "notes": []},
        }
        return filtered_vendor, {
            "kept_citations": len(kept_citations),
            "trusted_kept_citations": trusted_kept_citations,
            "signal_count": signal_count,
            "rejected_citations": len([item for item in item_judgements if item.get("judgement") == "drop"]),
        }

    def _judge_citation(
        self,
        citation: dict[str, Any],
        scope: dict[str, Any],
        requirement: dict[str, Any],
    ) -> tuple[str, str]:
        title = str(citation.get("title") or "")
        snippet = str(citation.get("snippet") or "")
        url = str(citation.get("url") or "")
        text = " ".join([title, snippet, url, requirement.get("title", ""), requirement.get("description", "")]).lower()

        if self._contains_negative_noise(text):
            return "drop", "内容明显属于无关模块或营销页面。"
        if self._is_homepage_like(url, title):
            return "drop", "首页/产品总览页缺少可验证细节。"

        must_have_terms = [term.lower() for term in scope.get("must_have_terms", []) or [] if term]
        ui_targets = [term.lower() for term in scope.get("ui_targets", []) or [] if term]
        feature_hits = sum(1 for term in self._feature_terms if term and term in text)
        must_have_hits = sum(1 for term in must_have_terms if term in text)
        ui_hits = sum(1 for term in ui_targets if term in text)
        score = feature_hits + must_have_hits * 2 + ui_hits

        if score <= 0:
            return "drop", "没有命中当前需求的核心能力词。"
        if self._infer_source_type(citation) != "official" and score < 2:
            return "drop", "二级资料且相关性不足。"
        return "keep", "证据与当前需求点直接相关。"

    def _normalize_scope(self, scope: dict[str, Any], requirement: dict[str, Any]) -> dict[str, Any]:
        title = requirement.get("title", "")
        description = requirement.get("description", "")
        combined = f"{title}\n{description}"
        must_have_terms = list(scope.get("must_have_terms") or [])
        ui_targets = list(scope.get("ui_targets") or [])
        if "未来审批" in combined and "未来审批" not in must_have_terms:
            must_have_terms.append("未来审批")
        if "审批人" in combined and "审批人" not in must_have_terms:
            must_have_terms.append("审批人")
        if "代理审批" in combined and "代理审批" not in must_have_terms:
            must_have_terms.append("代理审批")
        for keyword in ("流程监控", "审批面板", "流程图"):
            if keyword in combined and keyword not in ui_targets:
                ui_targets.append(keyword)
        return {
            "feature_intent": scope.get("feature_intent") or "requirement-focused competitor validation",
            "must_have_terms": self._dedupe(must_have_terms),
            "ui_targets": self._dedupe(ui_targets),
            "selected_vendors": list(scope.get("selected_vendors") or []),
            "selection_reason": scope.get("selection_reason") or "优先保留和当前需求点最相关的厂商证据。",
        }

    def _build_bundle_summary(self, bundle: dict[str, Any]) -> str:
        vendor_summaries = []
        for vendor in bundle.get("vendors", []) or []:
            if not vendor.get("feature_match"):
                continue
            vendor_summaries.append(
                f"{vendor.get('vendor', '未知厂商')} 命中了 {len(vendor.get('citations', []) or [])} 条高相关证据，"
                f"可借鉴 {('、'.join(vendor.get('borrowable_patterns', [])[:2]) or '相关审批规则设计')}。"
            )
        if not vendor_summaries:
            return "当前竞品证据不足，尚未形成可用的需求点级竞品分析。"
        return "；".join(vendor_summaries[:4])

    def _build_bundle_gaps(self, bundle: dict[str, Any]) -> list[str]:
        gaps = []
        for vendor in bundle.get("vendors", []) or []:
            if not vendor.get("feature_match"):
                gaps.append(f"{vendor.get('vendor', '未知厂商')} 缺少通过质量门禁的高相关证据。")
            verification = vendor.get("verification", {}) or {}
            if verification.get("status") == "unverified":
                gaps.append(f"{vendor.get('vendor', '未知厂商')} 仍未完成真实截图验证。")
        return self._dedupe(gaps)[:6]

    def _build_next_actions(self, bundle: dict[str, Any]) -> list[str]:
        actions = []
        for vendor in bundle.get("vendors", []) or []:
            if not vendor.get("feature_match"):
                actions.append(f"缩窄 {vendor.get('vendor', '竞品')} 的查询词，只保留和当前需求点直接相关的页面。")
            verification = vendor.get("verification", {}) or {}
            if verification.get("status") == "unverified" and (vendor.get("citations") or []):
                actions.append(f"为 {vendor.get('vendor', '竞品')} 发起真实验证截图任务。")
        if not actions:
            actions.append("当前竞品证据已通过质量门禁，可继续进入方案写作。")
        return self._dedupe(actions)[:6]

    def _build_improvement_actions(self, bundle: dict[str, Any]) -> list[str]:
        actions = []
        for vendor in bundle.get("vendors", []) or []:
            if not vendor.get("citations"):
                actions.append(f"补充 {vendor.get('vendor', '竞品')} 的官方帮助文档或真实页面截图。")
            if vendor.get("citations") and (vendor.get("verification", {}) or {}).get("status") == "unverified":
                actions.append(f"把 {vendor.get('vendor', '竞品')} 的公开证据升级为真实验证截图。")
        if not actions:
            actions.append("保持证据聚焦在当前需求点，避免再次混入营销与无关模块信息。")
        return self._dedupe(actions)[:6]

    def _contains_negative_noise(self, text: str) -> bool:
        lowered = str(text or "").lower()
        return any(term.lower() in lowered for term in self._negative_terms)

    def _is_homepage_like(self, url: str, title: str) -> bool:
        cleaned_url = str(url or "").strip().lower()
        cleaned_title = str(title or "").strip().lower()
        if not cleaned_url:
            return False
        path = re.sub(r"^https?://[^/]+", "", cleaned_url)
        if path in {"", "/"}:
            return True
        return any(marker in cleaned_title for marker in ["首页", "homepage", "官网"])

    def _infer_source_type(self, citation: dict[str, Any]) -> str:
        explicit = str(citation.get("source_type") or "").strip().lower()
        if explicit in {"official", "secondary", "internal_dossier"}:
            return explicit
        url = str(citation.get("url") or "").lower()
        official_markers = [
            "help.sap.com",
            "community.sap.com",
            "vip.kingdee.com",
            "help.open.kingdee.com",
            "open.kingdee.com",
            "ecologyhelp.weaver.com.cn",
            "weaver.com.cn",
            "help.seeyon.com",
            "seeyon.com",
        ]
        return "official" if any(marker in url for marker in official_markers) else "secondary"

    def _dedupe(self, items: list[str]) -> list[str]:
        ordered = []
        seen = set()
        for item in items:
            item = str(item or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered
