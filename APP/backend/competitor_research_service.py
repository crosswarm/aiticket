from __future__ import annotations

import html
import json
import re
import subprocess
import urllib.parse
import urllib.request
from urllib.parse import urljoin
from typing import Any


VENDOR_SOURCES = {
    "SAP": {
        "domains": ["help.sap.com", "community.sap.com"],
        "queries": ["workflow substitution approver", "substitute approver workflow"],
        "intent_queries": {
            "resubmit comment capture": [
                "workflow resubmit comment approval",
                "approval task comment note history",
            ],
        },
        "focus_terms": ["workflow", "substitute", "approver", "delegation", "future approver"],
    },
    "金蝶": {
        "domains": ["vip.kingdee.com", "help.open.kingdee.com", "open.kingdee.com"],
        "queries": ["审批委托 代理审批 工作流", "流程 审批 委托"],
        "intent_queries": {
            "resubmit comment capture": [
                "审批面板 重新提交 附言 说明",
                "审批意见 备注 补充说明",
            ],
        },
        "focus_terms": ["审批委托", "代理审批", "流程", "审批人"],
    },
    "泛微": {
        "domains": ["ecologyhelp.weaver.com.cn", "www.weaver.com.cn"],
        "queries": ["流程 审批 委托 代理", "工作流 代理审批"],
        "intent_queries": {
            "resubmit comment capture": [
                "审批面板 重新提交 备注 说明",
                "审批意见 补充说明 工作流",
            ],
        },
        "focus_terms": ["流程", "审批", "委托", "代理"],
    },
    "致远": {
        "domains": ["help.seeyon.com", "www.seeyon.com"],
        "queries": ["流程 审批 委托 代理", "工作流 代理审批"],
        "intent_queries": {
            "resubmit comment capture": [
                "审批面板 重新提交 附言 说明",
                "审批意见 补充说明 工作流",
            ],
        },
        "focus_terms": ["流程", "审批", "委托", "代理"],
    },
}


class CompetitorResearchService:
    def __init__(
        self,
        user_agent: str = "Mozilla/5.0 (compatible; aiticket/1.0)",
        command_runner: Any | None = None,
        page_fetcher: Any | None = None,
        command_timeout: int = 15,
    ) -> None:
        self.user_agent = user_agent
        self.command_runner = command_runner or subprocess.run
        self.page_fetcher = page_fetcher or self._fetch_page_html
        self.command_timeout = command_timeout

    def research(self, requirement: dict[str, Any], analysis_packet: dict[str, Any] | None = None, top_k: int = 3) -> dict[str, Any]:
        requirement = requirement or {}
        analysis_packet = analysis_packet or {}
        scope = self._build_scope(requirement, analysis_packet)
        selected_vendors = self._select_vendors(scope)

        vendor_results = []
        pending_questions = []
        for vendor in selected_vendors:
            config = VENDOR_SOURCES.get(vendor, {})
            result = self._research_vendor(vendor, config, scope, top_k=top_k)
            vendor_results.append(result)
            if not result.get("citations"):
                pending_questions.append(f"{vendor} 暂未命中充分公开资料，需人工补充验证。")

        summary = self._build_comparison_summary(vendor_results)
        return {
            "scope": scope,
            "summary": summary,
            "vendors": vendor_results,
            "comparison_takeaways": self._build_comparison_takeaways(vendor_results),
            "recommended_patterns": self._build_recommended_patterns(vendor_results),
            "architecture_overview": self._build_architecture_overview(vendor_results),
            "gaps": [item for item in pending_questions[:4]],
            "next_actions": self._build_next_actions(vendor_results),
            "pending_questions": pending_questions[:4],
        }

    def _research_vendor(self, vendor: str, config: dict[str, Any], scope: dict[str, Any], top_k: int = 3) -> dict[str, Any]:
        search_terms = (scope.get("must_have_terms", []) or [])[:3] + (scope.get("ui_targets", []) or [])[:2]
        search_terms = search_terms[:4] or ["流程审批", "代理审批"]
        vendor_queries = self._select_vendor_queries(config, scope)
        hits = []
        for domain in config.get("domains", []):
            for vendor_query in vendor_queries[:2]:
                query = f"site:{domain} {vendor_query} {' '.join(search_terms)}".strip()
                hits.extend(self._search_with_agent_reach(query, limit=top_k))
                if len(hits) < top_k * 2:
                    hits.extend(self._search_web(query, limit=top_k))
        hits = self._rank_hits(hits, config, scope, search_terms, top_k=top_k)

        citations = []
        borrowable_patterns = []
        scenarios = []
        raw_snippets = []
        for hit in hits[:top_k]:
            citations.append(
                {
                    "title": hit.get("title", ""),
                    "url": hit.get("url", ""),
                    "source_type": "official" if any(domain in hit.get("url", "") for domain in config.get("domains", [])) else "secondary",
                }
            )
            snippet = hit.get("snippet", "")
            if snippet:
                raw_snippets.append(snippet)
                for pattern in self._extract_patterns(snippet):
                    if pattern not in borrowable_patterns:
                        borrowable_patterns.append(pattern)
            if hit.get("title") and hit["title"] not in scenarios:
                scenarios.append(hit["title"])

        key_capabilities = self._extract_key_capabilities(raw_snippets, scenarios)
        ui_touchpoints = self._extract_ui_touchpoints(raw_snippets, scenarios)
        architecture_components = self._build_architecture_components(raw_snippets, ui_touchpoints)
        architecture_flow = [item["name"] for item in architecture_components]
        screenshot_targets = self._build_screenshot_targets(citations, raw_snippets, ui_touchpoints)
        key_images = self._build_key_images(citations)
        risks_or_limits = self._extract_risks_or_limits(raw_snippets, citations)
        implementation_summary = self._build_vendor_summary(
            vendor,
            key_capabilities,
            ui_touchpoints,
            borrowable_patterns,
            risks_or_limits,
            citations,
        )
        return {
            "vendor": vendor,
            "feature_scope": scope.get("feature_intent", "requirement-focused capability lookup"),
            "implementation_summary": implementation_summary,
            "scenarios": scenarios[:3],
            "key_capabilities": key_capabilities[:5],
            "ui_touchpoints": ui_touchpoints[:5],
            "architecture_components": architecture_components[:5],
            "architecture_flow": architecture_flow[:5],
            "key_images": key_images[:4],
            "screenshot_targets": screenshot_targets[:3],
            "borrowable_patterns": borrowable_patterns[:4],
            "risks_or_limits": risks_or_limits or ([] if citations else ["公开资料不足，需人工复核。"]),
            "citations": citations,
            "evidence_items": [
                {
                    "title": citation.get("title", ""),
                    "url": citation.get("url", ""),
                    "source_level": citation.get("source_type", "secondary"),
                    "snippet": citation.get("snippet", ""),
                }
                for citation in citations[:3]
            ],
            "verification": {"status": "unverified", "captures": [], "notes": []},
            "confidence": "high" if citations else "low",
        }

    def _rank_hits(
        self,
        hits: list[dict[str, str]],
        config: dict[str, Any],
        scope_or_search_terms: dict[str, Any] | list[str],
        search_terms: list[str] | None = None,
        top_k: int = 3,
    ) -> list[dict[str, str]]:
        if isinstance(scope_or_search_terms, dict):
            scope = scope_or_search_terms
            effective_search_terms = search_terms or []
        else:
            scope = {"feature_intent": "future approver lookup"}
            effective_search_terms = scope_or_search_terms or []

        deduped = []
        seen = set()
        for hit in hits:
            url = (hit.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(hit)

        domains = config.get("domains", []) or []
        delegation_terms = ["delegate", "delegation", "substitute", "substitution", "代理", "委托", "审批委托", "代理审批"]
        comment_terms = [
            "comment",
            "comments",
            "remark",
            "remarks",
            "note",
            "notes",
            "opinion",
            "resubmit",
            "resubmission",
            "resubmitted",
            "重新提交",
            "重提",
            "附言",
            "备注",
            "说明",
            "审批意见",
            "补充说明",
        ]
        workflow_terms = ["workflow", "approval", "approver", "待办", "流程", "审批", "监控", "流程图", "节点"]
        irrelevant_terms = ["supply chain", "production planning", "inventory", "transport", "procurement", "forecast", "deployment"]
        feature_intent = scope.get("feature_intent", "")
        primary_terms = comment_terms if feature_intent == "resubmit comment capture" else delegation_terms

        ranked = []
        for hit in deduped:
            title = (hit.get("title") or "").lower()
            snippet = (hit.get("snippet") or "").lower()
            url = (hit.get("url") or "").lower()
            text = f"{title} {snippet}"

            score = 0
            if any(domain in url for domain in domains):
                score += 3
            if any(term in title for term in primary_terms):
                score += 4
            if any(term in snippet for term in primary_terms):
                score += 2
            if any(term in title for term in workflow_terms):
                score += 3
            if any(term in snippet for term in workflow_terms):
                score += 1
            for term in effective_search_terms[:3]:
                lowered = term.lower()
                if lowered and lowered in text:
                    score += 1
            if not any(term in text for term in primary_terms):
                score -= 3
            if not any(term in text for term in workflow_terms):
                score -= 2
            if any(term in text for term in irrelevant_terms):
                score -= 4

            if score > 0:
                ranked.append((score, hit))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in ranked[:top_k]]

    def _search_with_agent_reach(self, query: str, limit: int = 3) -> list[dict[str, str]]:
        try:
            result = self.command_runner(
                ["mcporter", "call", "exa.web_search_exa", f"query={query}", f"numResults={limit}"],
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
        except Exception:
            return []

        if getattr(result, "returncode", 1) != 0:
            return []

        payload = self._extract_json_payload(getattr(result, "stdout", "") or "")
        if payload is not None:
            return self._normalize_search_results(payload, limit=limit)
        return self._normalize_exa_text_results(getattr(result, "stdout", "") or "", limit=limit)

    def _search_web(self, query: str, limit: int = 3) -> list[dict[str, str]]:
        try:
            url = f"https://duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html_text = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            return []

        results = []
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
            re.S,
        )
        for match in pattern.finditer(html_text):
            href = html.unescape(match.group("href"))
            title = self._strip_html(match.group("title"))
            snippet = self._strip_html(match.group("snippet"))
            if not href or href.startswith("/y.js") or not title:
                continue
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= limit:
                break
        return results

    def _extract_json_payload(self, raw: str) -> Any | None:
        text = (raw or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass

        match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except Exception:
            return None

    def _normalize_search_results(self, payload: Any, limit: int) -> list[dict[str, str]]:
        candidates = payload
        if isinstance(payload, dict):
            for key in ("results", "items", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = value
                    break

        if not isinstance(candidates, list):
            return []

        results: list[dict[str, str]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or ""
            url = item.get("url") or item.get("link") or item.get("id") or ""
            snippet = item.get("snippet") or item.get("text") or item.get("summary") or ""
            if not title or not url:
                continue
            results.append(
                {
                    "title": self._strip_html(str(title)),
                    "url": str(url).strip(),
                    "snippet": self._strip_html(str(snippet)),
                }
            )
            if len(results) >= limit:
                break
        return results

    def _normalize_exa_text_results(self, raw: str, limit: int) -> list[dict[str, str]]:
        text = (raw or "").strip()
        if not text:
            return []

        chunks = re.split(r"\n(?=Title:\s)", text)
        results: list[dict[str, str]] = []
        for chunk in chunks:
            title_match = re.search(r"^Title:\s*(.+)$", chunk, re.M)
            url_match = re.search(r"^URL:\s*(.+)$", chunk, re.M)
            text_match = re.search(r"^Text:\s*(.+)$", chunk, re.S | re.M)
            if not title_match or not url_match:
                continue
            snippet = ""
            if text_match:
                snippet = text_match.group(1).strip()
            else:
                author_match = re.search(r"^Author:\s*(.+)$", chunk, re.M)
                snippet = author_match.group(1).strip() if author_match else ""
            results.append(
                {
                    "title": self._strip_html(title_match.group(1).strip()),
                    "url": url_match.group(1).strip(),
                    "snippet": self._strip_html(snippet),
                }
            )
            if len(results) >= limit:
                break
        return results

    def _extract_keywords(self, text: str) -> list[str]:
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{3,}", text or "")
        ordered = []
        seen = set()
        for token in tokens:
            token = token.strip()
            if not token or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered[:8]

    def _select_vendor_queries(self, config: dict[str, Any], scope: dict[str, Any]) -> list[str]:
        feature_intent = scope.get("feature_intent", "")
        intent_queries = (config.get("intent_queries", {}) or {}).get(feature_intent) or []
        return intent_queries or config.get("queries", []) or []

    def _build_scope(self, requirement: dict[str, Any], analysis_packet: dict[str, Any]) -> dict[str, Any]:
        title = requirement.get("title", "")
        description = requirement.get("description", "")
        topic_names = analysis_packet.get("topic_names", []) or []
        combined = "\n".join([title, description, " ".join(topic_names)])
        resubmit_terms = ["重新提交", "重提", "附言", "备注", "说明", "审批意见", "补充说明"]
        resubmit_ui_terms = ["审批面板", "审批页", "待办处理区", "重提弹窗"]

        feature_intent = "requirement-focused competitor lookup"
        if (
            any(keyword in combined for keyword in ["重新提交", "重提"])
            and any(keyword in combined for keyword in ["附言", "备注", "说明", "审批意见", "补充说明"])
        ):
            feature_intent = "resubmit comment capture"
        elif any(keyword in combined for keyword in ["未来审批", "审批人", "代理审批", "审批委托"]):
            feature_intent = "future approver lookup"
        elif "流程监控" in combined:
            feature_intent = "workflow monitoring enhancement"

        must_have_terms = []
        ui_targets = []
        keywords = ["未来审批", "审批人", "代理审批", "审批委托", "工作流", "流程监控", "流程图", "审批面板"]
        if feature_intent == "resubmit comment capture":
            keywords = ["重新提交", "重提", "附言", "备注", "说明", "审批意见", "补充说明", "审批面板", "重提弹窗"]
        for keyword in keywords:
            if keyword in combined:
                if keyword in {"流程监控", "流程图", "审批面板"}:
                    ui_targets.append(keyword)
                elif keyword in {"重提弹窗"}:
                    ui_targets.append(keyword)
                else:
                    must_have_terms.append(keyword)

        if not must_have_terms:
            must_have_terms = self._extract_keywords(" ".join([title, description]))[:4]
        if feature_intent == "resubmit comment capture":
            for keyword in ["重新提交", "附言", "说明"]:
                if keyword in combined and keyword not in must_have_terms:
                    must_have_terms.append(keyword)
            for keyword in resubmit_ui_terms:
                if keyword in combined and keyword not in ui_targets:
                    ui_targets.append(keyword)
        return {
            "feature_intent": feature_intent,
            "must_have_terms": must_have_terms[:5],
            "ui_targets": ui_targets[:4],
            "selected_vendors": [],
            "selection_reason": "根据当前需求的功能意图、场景词和页面落点动态收紧厂商范围。",
        }

    def _select_vendors(self, scope: dict[str, Any]) -> list[str]:
        terms = " ".join((scope.get("must_have_terms", []) or []) + (scope.get("ui_targets", []) or []))
        if scope.get("feature_intent") == "resubmit comment capture":
            return ["SAP", "金蝶", "泛微", "致远"]
        if any(keyword in terms for keyword in ["未来审批", "审批人", "代理审批", "审批委托"]):
            return ["SAP", "金蝶", "泛微", "致远"]
        if "流程监控" in terms:
            return ["SAP", "金蝶", "泛微"]
        return ["SAP", "金蝶"]

    def _extract_patterns(self, text: str) -> list[str]:
        patterns = []
        mapping = {
            "代理审批": "代理审批配置",
            "委托": "审批委托规则",
            "substitute": "替代审批人配置",
            "delegation": "审批委托链路",
            "resubmit": "重提补充说明",
            "remark": "补充说明/备注记录",
            "comment": "审批意见/说明留痕",
            "note": "备注/说明记录",
            "工作流": "工作流内嵌审批配置",
            "流程": "流程节点审批策略",
        }
        lowered = (text or "").lower()
        for keyword, label in mapping.items():
            if keyword.lower() in lowered and label not in patterns:
                patterns.append(label)
        return patterns

    def _extract_key_capabilities(self, snippets: list[str], scenarios: list[str]) -> list[str]:
        text = " ".join((snippets or []) + (scenarios or []))
        mapping = [
            ("代理审批", "支持代理审批/替代审批人配置"),
            ("委托", "支持审批委托与代理人规则"),
            ("substitute", "支持替代审批人配置"),
            ("delegation", "支持审批委托生效链路"),
            ("comment", "支持审批意见/补充说明录入"),
            ("remark", "支持补充说明/备注展示"),
            ("note", "支持备注/说明留痕"),
            ("resubmit", "支持重新提交时补充说明"),
            ("附言", "支持审批附言/补充说明"),
            ("说明", "支持说明透传与展示"),
            ("流程监控", "支持流程监控与在途实例查看"),
            ("监控图", "支持流程监控图查看运行态链路"),
            ("流程图", "支持流程图/链路可视化"),
            ("下一个节点", "支持查看下一节点/后续审批环节"),
            ("未来", "支持未来审批链路或候选审批人可视化"),
            ("仿真", "支持流程仿真/预测能力"),
            ("待办", "支持与待办处理区联动"),
        ]
        return [label for keyword, label in mapping if keyword.lower() in text.lower()]

    def _extract_ui_touchpoints(self, snippets: list[str], scenarios: list[str]) -> list[str]:
        text = " ".join((snippets or []) + (scenarios or []))
        mapping = [
            ("流程监控", "流程监控页"),
            ("监控图", "流程监控图"),
            ("流程图", "流程图/链路图"),
            ("审批面板", "审批面板"),
            ("approval panel", "审批面板"),
            ("comment", "审批意见/说明区域"),
            ("remark", "备注/说明区域"),
            ("dialog", "重提弹窗"),
            ("待办", "待办处理区"),
            ("流程管理", "流程管理后台"),
            ("workflow administration", "流程管理后台"),
            ("流程模板", "流程模板/设计器"),
            ("工作流管理", "工作流管理台"),
            ("运行", "运行态查看页"),
            ("组织", "组织/人员代理配置页"),
        ]
        return [label for keyword, label in mapping if keyword.lower() in text.lower()]

    def _build_architecture_components(self, snippets: list[str], ui_touchpoints: list[str]) -> list[dict[str, str]]:
        text = " ".join(snippets or [])
        components = []
        if any(item in ui_touchpoints for item in ["流程监控页", "流程监控图", "流程图/链路图"]):
            components.append({"name": "监控/链路展示层", "role": "展示当前节点、未来审批链路和流程路径。"})
        if any(keyword in text.lower() for keyword in ["delegate", "delegation", "委托", "代理审批", "substitute"]):
            components.append({"name": "代理审批规则层", "role": "维护代理人、委托关系和替代审批规则。"})
        if any(keyword in text.lower() for keyword in ["comment", "remark", "note", "附言", "说明", "审批意见"]):
            components.append({"name": "补充说明输入层", "role": "承接审批面板或重提弹窗中的附言/说明录入与展示。"})
        if any(keyword in text.lower() for keyword in ["workflow", "流程", "引擎"]):
            components.append({"name": "流程引擎层", "role": "根据流程定义、路由规则和节点状态计算后续审批链路。"})
        if any(keyword in text.lower() for keyword in ["simulate", "simulation", "预测", "仿真", "下一个节点", "未来"]):
            components.append({"name": "预测/模拟层", "role": "计算未来节点、候选审批人和链路解释。"})
        if any(keyword in text.lower() for keyword in ["history", "audit", "日志", "留痕", "记录"]):
            components.append({"name": "审批留痕层", "role": "记录补充说明、审批意见与历史轨迹，供后续审批人查看。"})
        if any(keyword in text.lower() for keyword in ["task", "待办", "组织", "role", "用户"]):
            components.append({"name": "组织与待办集成层", "role": "结合组织权限、待办处理和审批人替代关系输出最终可见结果。"})
        return components

    def _build_screenshot_targets(
        self,
        citations: list[dict[str, str]],
        snippets: list[str],
        ui_touchpoints: list[str],
    ) -> list[dict[str, str]]:
        joined = " ".join(snippets or [])
        focus_candidates = []
        if "流程监控图" in ui_touchpoints or "流程图/链路图" in ui_touchpoints:
            focus_candidates.append(("流程监控图/流程图", "捕捉未来节点路径、当前节点状态和候选审批人展示位。"))
        if "审批面板" in ui_touchpoints or "待办处理区" in ui_touchpoints:
            focus_candidates.append(("审批面板/待办区", "捕捉代理审批、替代审批人或委托规则在审批界面的呈现方式。"))
        if "审批意见/说明区域" in ui_touchpoints or any(keyword in joined.lower() for keyword in ["comment", "remark", "note", "附言", "说明"]):
            focus_candidates.append(("审批面板说明区/重提弹窗", "捕捉重新提交时的附言说明输入区、历史意见展示区和透传效果。"))
        if any(keyword in joined.lower() for keyword in ["delegate", "delegation", "委托", "代理审批", "substitute"]):
            focus_candidates.append(("委托/代理规则配置页", "捕捉代理审批规则、替代审批人配置和生效条件。"))
        if not focus_candidates:
            focus_candidates.append(("流程管理/帮助中心页面", "优先截取最能说明流程委托和未来审批链路的页面区域。"))

        targets = []
        for index, citation in enumerate(citations[: len(focus_candidates)]):
            focus_area, reason = focus_candidates[index]
            targets.append(
                {
                    "title": citation.get("title", "") or focus_area,
                    "url": citation.get("url", ""),
                    "focus_area": focus_area,
                    "reason": reason,
                }
            )
        return targets

    def _build_key_images(self, citations: list[dict[str, str]]) -> list[dict[str, str]]:
        images = []
        seen = set()
        for citation in citations[:3]:
            url = citation.get("url", "")
            if not url:
                continue
            html_text = self.page_fetcher(url)
            for image in self._extract_image_candidates(url, html_text):
                image_url = image.get("image_url", "")
                if not image_url or image_url in seen:
                    continue
                seen.add(image_url)
                images.append(
                    {
                        "title": citation.get("title", "") or image.get("title", "关键图片"),
                        "page_url": url,
                        "image_url": image_url,
                        "source": image.get("source", "article"),
                        "alt": image.get("alt", ""),
                    }
                )
                if len(images) >= 4:
                    return images
        return images

    def _fetch_page_html(self, url: str) -> str:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=8) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    return ""
                return resp.read().decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def _extract_image_candidates(self, page_url: str, html_text: str) -> list[dict[str, str]]:
        if not html_text:
            return []

        candidates = []
        meta_patterns = [
            (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', "og:image"),
            (r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']', "twitter:image"),
        ]
        for pattern, source in meta_patterns:
            for match in re.finditer(pattern, html_text, re.I):
                image_url = urljoin(page_url, html.unescape(match.group(1).strip()))
                if image_url:
                    candidates.append({"image_url": image_url, "source": source, "title": "文章主图", "alt": ""})

        img_pattern = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", re.I)
        alt_pattern = re.compile(r'alt=[\"\']([^\"\']*)[\"\']', re.I)
        for match in img_pattern.finditer(html_text):
            raw_tag = match.group(0)
            raw_src = match.group(1).strip()
            lowered = raw_src.lower()
            if any(token in lowered for token in ["logo", "icon", "sprite", "avatar", ".svg"]):
                continue
            image_url = urljoin(page_url, html.unescape(raw_src))
            alt_match = alt_pattern.search(raw_tag)
            candidates.append(
                {
                    "image_url": image_url,
                    "source": "article:img",
                    "title": "正文图片",
                    "alt": (alt_match.group(1).strip() if alt_match else ""),
                }
            )

        deduped = []
        seen = set()
        for item in candidates:
            image_url = item.get("image_url", "")
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            deduped.append(item)
        return deduped[:4]

    def _extract_risks_or_limits(self, snippets: list[str], citations: list[dict[str, str]]) -> list[str]:
        joined = " ".join(snippets or []).lower()
        risks = []
        if "谨慎" in joined or "不可逆" in joined:
            risks.append("公开资料提示部分运行态干预操作不可逆，需明确权限与审计边界。")
        if "仿真" in joined or "预测" in joined:
            risks.append("若依赖预测/仿真能力，需要明确预测结果与实际审批链路的差异解释。")
        if not citations:
            risks.append("当前竞品资料不足，方案判断需人工复核。")
        return risks[:3]

    def _build_vendor_summary(
        self,
        vendor: str,
        key_capabilities: list[str],
        ui_touchpoints: list[str],
        borrowable_patterns: list[str],
        risks_or_limits: list[str],
        citations: list[dict[str, str]],
    ) -> str:
        if not citations:
            return f"暂未检索到 {vendor} 公开可用的明确实现描述。"
        capability_text = "、".join(key_capabilities[:3]) or "提供流程审批相关能力"
        ui_text = "、".join(ui_touchpoints[:2]) or "流程管理界面"
        pattern_text = "、".join(borrowable_patterns[:2]) or "可进一步人工抽取"
        risk_text = risks_or_limits[0] if risks_or_limits else "需结合本产品边界进一步确认。"
        return f"{vendor} 公开资料显示其已 {capability_text}，主要落在 {ui_text}，可借鉴 {pattern_text}；{risk_text}"

    def _build_comparison_summary(self, vendor_results: list[dict[str, Any]]) -> str:
        available = [item for item in vendor_results if item.get("citations")]
        if not available:
            return "暂未检索到足够的竞品公开资料。"
        vendors = "、".join(item["vendor"] for item in available[:4])
        common_patterns = self._build_recommended_patterns(vendor_results)
        pattern_text = "、".join(common_patterns[:3]) or "代理审批与流程链路可视化"
        return f"{vendors} 的公开资料都指向同一条主线：通过代理/委托规则层承接审批人替代关系，并在监控/待办/流程图等界面解释未来审批链路。当前最值得借鉴的是 {pattern_text}。"

    def _build_comparison_takeaways(self, vendor_results: list[dict[str, Any]]) -> list[str]:
        takeaways = []
        for vendor in vendor_results:
            if not vendor.get("citations"):
                continue
            capabilities = "、".join((vendor.get("key_capabilities") or [])[:2]) or "流程委托能力"
            touchpoints = "、".join((vendor.get("ui_touchpoints") or [])[:2]) or "流程管理界面"
            takeaways.append(f"{vendor.get('vendor')}: 主要通过 {touchpoints} 承载 {capabilities}。")
        return takeaways[:4]

    def _build_recommended_patterns(self, vendor_results: list[dict[str, Any]]) -> list[str]:
        patterns = []
        for vendor in vendor_results:
            for item in vendor.get("borrowable_patterns", []) or []:
                if item not in patterns:
                    patterns.append(item)
        return patterns[:6]

    def _build_architecture_overview(self, vendor_results: list[dict[str, Any]]) -> dict[str, Any]:
        flow = []
        notes = []
        for vendor in vendor_results:
            for item in vendor.get("architecture_flow", []) or []:
                if item not in flow:
                    flow.append(item)
            if vendor.get("citations"):
                notes.append(f"{vendor.get('vendor')} 强调 {'、'.join((vendor.get('ui_touchpoints') or [])[:2]) or '流程管理界面'} 与规则层联动。")
        return {
            "title": "竞品共性参考架构",
            "flow": flow[:6],
            "notes": notes[:4],
        }

    def _build_next_actions(self, vendor_results: list[dict[str, Any]]) -> list[str]:
        actions = []
        for vendor in vendor_results:
            if not vendor.get("citations"):
                actions.append(f"补充 {vendor.get('vendor', '竞品')} 的官方帮助文档或真实截图。")
                continue
            targets = vendor.get("screenshot_targets", []) or []
            focus_area = (targets[0] if targets else {}).get("focus_area", "关键页面")
            actions.append(f"优先验证 {vendor.get('vendor', '竞品')} 的 {focus_area}。")
        return actions[:6]

    # ------------------------------------------------------------------
    # Phase 2 additions (S1 stubs — full implementation in S2)
    # ------------------------------------------------------------------

    def research_by_feature(
        self,
        vendor_id: str,
        feature_id: str,
        query_override: str | None = None,
    ) -> list[dict[str, Any]]:
        """Layer A: targeted search for a specific vendor + feature combination.

        Uses agent-reach/Exa for public data, scoped to the vendor's domains.
        Returns a list of CompetitorEvidence dicts.

        Parameters
        ----------
        vendor_id:
            Registry vendor id ('sap', 'kingdee', 'weaver', 'seeyon').
        feature_id:
            Feature taxonomy id ('workflow_approval', 'delegation', …).
        query_override:
            Optional custom search query; if None, one is built from the
            vendor config and feature taxonomy.
        """
        # 1. Map registry vendor_id to the legacy VENDOR_SOURCES key
        _id_to_key = {
            "sap": "SAP",
            "kingdee": "金蝶",
            "weaver": "泛微",
            "seeyon": "致远",
        }
        vendor_key = _id_to_key.get(vendor_id)
        if vendor_key is None:
            return []

        vendor_config = VENDOR_SOURCES.get(vendor_key, {})
        domains = vendor_config.get("domains", [])

        # 2. Build search terms from feature taxonomy (placeholder map for S1)
        _feature_terms: dict[str, list[str]] = {
            "workflow_approval": ["审批流程", "workflow approval"],
            "delegation": ["审批委托", "代理审批", "delegation", "substitute approver"],
            "form_design": ["表单设计", "form design", "form template"],
            "branch_condition": ["条件分支", "branch condition", "route map"],
            "notification": ["消息通知", "workflow notification"],
            "print_integration": ["打印集成", "print template"],
            "mobile_approval": ["移动审批", "mobile approval"],
            "api_integration": ["API集成", "OpenAPI", "REST API"],
            "performance_monitor": ["流程监控", "process monitoring", "SLA"],
            "signature": ["电子签名", "e-signature", "electronic signature"],
        }
        feature_terms = _feature_terms.get(feature_id, [feature_id])

        # 3. Build queries and call agent-reach
        collected_at = __import__("datetime").date.today().isoformat()
        all_hits: list[dict[str, str]] = []

        if query_override:
            queries = [query_override]
        else:
            queries = []
            for domain in domains[:2]:
                base_term = feature_terms[0]
                queries.append(f"site:{domain} {base_term}")
            # Open web query with both zh and en terms
            queries.append(f"{vendor_key} {' '.join(feature_terms[:2])}")

        for query in queries[:3]:
            all_hits.extend(self._search_with_agent_reach(query, limit=3))
            if len(all_hits) < 3:
                all_hits.extend(self._search_web(query, limit=3))

        # 4. Deduplicate
        seen_urls: set[str] = set()
        deduped: list[dict[str, str]] = []
        for hit in all_hits:
            url = (hit.get("url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                deduped.append(hit)

        # 5. Structure as CompetitorEvidence
        evidence: list[dict[str, Any]] = []
        for hit in deduped[:5]:
            url = hit.get("url", "")
            is_official = any(d in url for d in domains)
            confidence: str
            if is_official:
                confidence = "high"
            elif any(term.lower() in (hit.get("snippet") or "").lower() for term in feature_terms):
                confidence = "medium"
            else:
                confidence = "low"

            evidence.append(
                {
                    "vendor_id": vendor_id,
                    "feature_id": feature_id,
                    "source_type": "help_doc" if is_official else "blog",
                    "title": hit.get("title", ""),
                    "url": url,
                    "content_snippet": (hit.get("snippet") or "")[:500],
                    "collected_at": collected_at,
                    "confidence": confidence,
                }
            )

        # 6. Quality filter: keep medium/high confidence items; fall back to all if none pass
        filtered = [e for e in evidence if e["confidence"] in ("high", "medium")]
        return filtered if filtered else evidence

    def research_with_cache(
        self,
        requirement: dict[str, Any],
        feature_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Cache-first research: check exploration assets and KB for competitor data.

        Priority order for each (vendor, feature) pair:
        1. ExplorationAssetRetriever — KB docs, screenshots, prototypes, feature_matrix
        2. KB cache (competitor_evidence DB) — previously persisted web search results
        3. research_by_feature() — live web search (only on cache miss / stale data)

        Annotates each result with a 'freshness' field and 'source' field.
        """
        from datetime import datetime, timedelta

        if feature_ids is None:
            from competitor_account_manager import CompetitorAccountManager
            mgr = CompetitorAccountManager()
            req_text = (requirement.get("title", "") + " " + requirement.get("description", "")).strip()
            feature_ids = mgr.match_requirement_to_features(req_text)

        if not feature_ids:
            feature_ids = ["workflow_approval", "delegation"]

        _id_to_key = {
            "sap": "SAP",
            "kingdee": "金蝶",
            "weaver": "泛微",
            "seeyon": "致远",
        }
        vendor_ids = list(_id_to_key.keys())

        # Phase 1: Check ExplorationAssetRetriever for pre-explored data
        exploration_covered: set[tuple[str, str]] = set()
        try:
            from exploration_asset_retriever import ExplorationAssetRetriever
            retriever = ExplorationAssetRetriever()
            for vendor_id in vendor_ids:
                for feature_id in feature_ids:
                    asset = retriever.retrieve(vendor_id, feature_id)
                    if asset and asset.get("freshness") in ("fresh", "stale"):
                        exploration_covered.add((vendor_id, feature_id))
        except Exception:
            pass

        # Phase 2: For cells not covered by exploration, fall back to KB cache + web search
        results: list[dict[str, Any]] = []
        for vendor_id in vendor_ids:
            for feature_id in feature_ids:
                if (vendor_id, feature_id) in exploration_covered:
                    # Already covered by exploration assets — synthesize a summary evidence entry
                    try:
                        asset = retriever.retrieve(vendor_id, feature_id)  # type: ignore[possibly-undefined]
                        results.append({
                            "vendor_id": vendor_id,
                            "feature_id": feature_id,
                            "source_type": "kb_exploration",
                            "title": f"{asset.get('vendor_name', vendor_id)} - {asset.get('feature_name', feature_id)}",
                            "url": "",
                            "content_snippet": (asset.get("kb_summary") or asset.get("key_findings") or "")[:500],
                            "collected_at": asset.get("explored_at", ""),
                            "confidence": asset.get("confidence", "medium"),
                            "source": "kb_exploration",
                            "support_status": asset.get("support_status", "unknown"),
                            "screenshots_count": len(asset.get("screenshots", [])),
                            "prototypes_count": len(asset.get("prototypes", [])),
                            "freshness": asset.get("freshness", "fresh"),
                        })
                    except Exception:
                        pass
                    continue

                cached = self._search_kb_cache(vendor_id, feature_id)
                if cached and self._is_fresh(cached, max_age_days=30):
                    for c in cached:
                        c["source"] = "kb_cache"
                    results.extend(cached)
                else:
                    fresh = self.research_by_feature(vendor_id, feature_id)
                    for ev in fresh:
                        ev["source"] = "web_search"
                        self._persist_to_kb(ev)
                    results.extend(fresh)

        # Annotate freshness on results that don't already have it
        now = datetime.now()
        for r in results:
            if r.get("freshness"):
                continue
            collected_at = r.get("collected_at", "")
            try:
                age_days = (now - datetime.fromisoformat(collected_at)).days
            except Exception:
                age_days = 999
            if age_days <= 30:
                r["freshness"] = "fresh"
            elif age_days <= 90:
                r["freshness"] = "stale"
            else:
                r["freshness"] = "expired"

        return results

    def _get_competitor_kb(self) -> "KnowledgeHybridIndex":
        """Return a KnowledgeHybridIndex instance scoped to competitor evidence."""
        if not hasattr(self, "_competitor_kb") or self._competitor_kb is None:
            from pathlib import Path
            from kb_hybrid_index import KnowledgeHybridIndex
            _base = Path(__file__).parent
            _data_dir = _base / "data" / "competitor_kb"
            _data_dir.mkdir(parents=True, exist_ok=True)
            self._competitor_kb: KnowledgeHybridIndex = KnowledgeHybridIndex(
                sqlite_path=_data_dir / "competitor_evidence.db",
                chroma_path=_data_dir / "chroma",
                collection_name="competitor_evidence",
            )
        return self._competitor_kb

    def _persist_to_kb(self, evidence: dict[str, Any]) -> None:
        """Write a CompetitorEvidence item to the competitor KB (upsert by URL hash)."""
        import json as _json
        kb = self._get_competitor_kb()
        url = evidence.get("url", "")
        vendor_id = evidence.get("vendor_id", "")
        feature_id = evidence.get("feature_id", "")
        url_hash = str(abs(hash(url)) % (10 ** 8))
        content_id = f"competitor:{vendor_id}:{feature_id}:{url_hash}"
        # Encode all competitor-specific fields into summary as JSON for later retrieval
        summary_payload = _json.dumps({
            "vendor_id": vendor_id,
            "feature_id": feature_id,
            "source_type": evidence.get("source_type", ""),
            "url": url,
            "title": evidence.get("title", ""),
            "collected_at": evidence.get("collected_at", ""),
            "confidence": evidence.get("confidence", ""),
        }, ensure_ascii=False)
        item: dict[str, Any] = {
            "content_id": content_id,
            "source_kind": "competitor",
            "name": content_id,
            "summary": summary_payload,
            "source_rel_path": url,
            "citation_label": f"{vendor_id}:{feature_id}",
            "l1_module": vendor_id,
            "l2_module": feature_id,
            "doc_type": evidence.get("source_type", ""),
            "keywords": [vendor_id, feature_id],
        }
        text = evidence.get("content_snippet", "") or evidence.get("title", "") or content_id
        try:
            kb.add_item(item, text)
        except Exception:
            pass  # KB write failures must not break the research flow

    def _search_kb_cache(self, vendor_id: str, feature_id: str) -> list[dict[str, Any]]:
        """Search KB for cached competitor evidence, returning CompetitorEvidence dicts."""
        import json as _json
        kb = self._get_competitor_kb()
        query = f"{vendor_id} {feature_id}"
        try:
            hits = kb.search(query, top_k=10, source_kind="competitor")
        except Exception:
            return []

        results: list[dict[str, Any]] = []
        for hit in hits:
            # Filter to exact vendor + feature match via l1_module/l2_module or summary
            hit_l1 = hit.get("l1_module", "")
            hit_l2 = hit.get("l2_module", "")
            hit_label = hit.get("citation_label", "")
            expected_label = f"{vendor_id}:{feature_id}"
            if not (
                (hit_l1 == vendor_id and hit_l2 == feature_id)
                or hit_label == expected_label
            ):
                continue
            # Reconstruct CompetitorEvidence from summary JSON
            try:
                meta = _json.loads(hit.get("summary", "{}") or "{}")
            except Exception:
                meta = {}
            ev: dict[str, Any] = {
                "vendor_id": meta.get("vendor_id", vendor_id),
                "feature_id": meta.get("feature_id", feature_id),
                "source_type": meta.get("source_type", hit.get("doc_type", "")),
                "title": meta.get("title", hit.get("name", "")),
                "url": meta.get("url", hit.get("source_rel_path", "")),
                "content_snippet": hit.get("chunk_text", ""),
                "collected_at": meta.get("collected_at", ""),
                "confidence": meta.get("confidence", "low"),
            }
            results.append(ev)
        return results

    def _is_fresh(self, results: list[dict[str, Any]], max_age_days: int = 30) -> bool:
        """Return True if any result has a collected_at within max_age_days."""
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=max_age_days)).date().isoformat()
        return any(
            (r.get("collected_at") or "") >= cutoff
            for r in results
        )

    # ------------------------------------------------------------------
    # Layer B — opencli / Playwright authenticated feature exploration
    # ------------------------------------------------------------------

    def explore_feature_with_opencli(self, vendor_id: str, feature_id: str) -> dict[str, Any]:
        """Layer B: Explore a competitor's specific feature via Playwright (authenticated).

        Flow:
        1. Load vendor config from registry
        2. Get feature module's help_url
        3. Check account profile status via CompetitorAccountManager
        4. If active: launch Playwright with storageState -> navigate -> screenshot + capture network
        5. If expired/missing: mark degraded=True, skip Playwright (graceful fallback to Layer A)
        6. Store captures to data_cache/competitor_validation/captures/<vendor>/<feature>/
        7. Return exploration result dict
        """
        from pathlib import Path
        from datetime import date

        explored_at = date.today().isoformat()

        _degraded_result: dict[str, Any] = {
            "vendor_id": vendor_id,
            "feature_id": feature_id,
            "explored": False,
            "screenshots": [],
            "api_captures": [],
            "page_title": "",
            "page_text_snippet": "",
            "explored_at": explored_at,
            "degraded": True,
            "error": None,
        }

        # 1. Load vendor + feature config
        try:
            from competitor_account_manager import CompetitorAccountManager
            mgr = CompetitorAccountManager()
        except Exception as exc:
            _degraded_result["error"] = f"failed to import CompetitorAccountManager: {exc}"
            return _degraded_result

        vendor = mgr.get_vendor(vendor_id)
        if not vendor:
            _degraded_result["error"] = f"vendor '{vendor_id}' not found in registry"
            return _degraded_result

        feature_module = next(
            (f for f in mgr.get_feature_modules(vendor_id) if f.get("id") == feature_id),
            None,
        )
        target_url = (feature_module or {}).get("help_url", "")
        if not target_url:
            # Fall back to vendor product domain
            target_url = (vendor.get("domains") or {}).get("help", "")
            if target_url and not target_url.startswith("http"):
                target_url = f"https://{target_url}"

        if not target_url:
            _degraded_result["error"] = f"no target URL found for {vendor_id}/{feature_id}"
            return _degraded_result

        # 2. Check account status
        account_status = mgr.check_account_status(vendor_id)
        account = mgr.get_active_account(vendor_id)

        if account_status != "active" or account is None:
            _degraded_result["error"] = (
                f"account status={account_status!r} for {vendor_id}; requires_manual_login"
            )
            return _degraded_result

        profile_path_str: str = account.get("profile_path", "")

        # Resolve profile path relative to project root (APP/backend/../..)
        _backend_dir = Path(__file__).parent
        _project_root = _backend_dir.parent.parent
        profile_path = (
            _project_root / profile_path_str if profile_path_str else None
        )
        storage_state = str(profile_path) if (profile_path and profile_path.exists()) else None

        # 3. Prepare capture directories
        captures_base = _project_root / "data_cache" / "competitor_validation" / "captures"
        api_captures_base = _project_root / "data_cache" / "competitor_validation" / "api_captures"
        capture_dir = captures_base / vendor_id / feature_id
        capture_dir.mkdir(parents=True, exist_ok=True)
        api_captures_dir = api_captures_base / vendor_id
        api_captures_dir.mkdir(parents=True, exist_ok=True)

        # 4. Playwright exploration
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            _degraded_result["error"] = "playwright not installed"
            return _degraded_result

        screenshot_paths: list[str] = []
        api_captures: list[dict[str, Any]] = []
        page_title = ""
        page_text_snippet = ""
        exploration_error: str | None = None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                ctx = browser.new_context(
                    storage_state=storage_state,
                    ignore_https_errors=True,
                )

                # Intercept network requests for API discovery
                intercepted: list[dict[str, Any]] = []

                def _on_request(request: Any) -> None:
                    if request.resource_type in ("xhr", "fetch"):
                        intercepted.append({
                            "url": request.url,
                            "method": request.method,
                            "resource_type": request.resource_type,
                        })

                ctx.on("request", _on_request)

                page = ctx.new_page()
                try:
                    page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    # Partial load is acceptable — capture what we have
                    pass

                page_title = page.title() or ""
                # Extract up to 500 chars of visible body text
                try:
                    body_text = page.inner_text("body") or ""
                    page_text_snippet = body_text[:500]
                except Exception:
                    page_text_snippet = ""

                # Full-page screenshot
                screenshot_file = capture_dir / "page_01.png"
                try:
                    page.screenshot(path=str(screenshot_file), full_page=True)
                    screenshot_paths.append(
                        f"captures/{vendor_id}/{feature_id}/page_01.png"
                    )
                except Exception as exc:
                    exploration_error = f"screenshot failed: {exc}"

                # Save meta.json
                import json as _json
                meta = {
                    "explored_at": explored_at,
                    "page_title": page_title,
                    "page_url": target_url,
                }
                try:
                    (capture_dir / "meta.json").write_text(
                        _json.dumps(meta, ensure_ascii=False, indent=2)
                    )
                except Exception:
                    pass

                api_captures = intercepted[:50]  # cap at 50 entries

                ctx.close()
                browser.close()

        except Exception as exc:
            exploration_error = str(exc)

        # 5. Persist API captures
        if api_captures:
            import json as _json
            api_file = api_captures_dir / f"{feature_id}.json"
            try:
                api_file.write_text(
                    _json.dumps(
                        {
                            "vendor_id": vendor_id,
                            "feature_id": feature_id,
                            "explored_at": explored_at,
                            "requests": api_captures,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            except Exception:
                pass

        explored = bool(screenshot_paths) and exploration_error is None

        return {
            "vendor_id": vendor_id,
            "feature_id": feature_id,
            "explored": explored,
            "screenshots": screenshot_paths,
            "api_captures": api_captures,
            "page_title": page_title,
            "page_text_snippet": page_text_snippet,
            "explored_at": explored_at,
            "degraded": not explored,
            "error": exploration_error,
        }

    def run_batch_exploration(
        self,
        vendor_ids: list[str] | None = None,
        feature_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Batch explore all vendor x feature combos. For weekly launchd runs.

        Skips vendors with expired/missing accounts gracefully (degraded=True).
        Returns list of exploration result dicts.
        """
        from datetime import date

        try:
            from competitor_account_manager import CompetitorAccountManager
            mgr = CompetitorAccountManager()
        except Exception as exc:
            return [{"error": f"failed to import CompetitorAccountManager: {exc}", "explored": False}]

        explored_at = date.today().isoformat()
        results: list[dict[str, Any]] = []
        vendors = vendor_ids or [v["id"] for v in mgr.get_all_vendors()]

        for vid in vendors:
            account_status = mgr.check_account_status(vid)
            fids = feature_ids or [f["id"] for f in mgr.get_feature_modules(vid)]

            if account_status != "active":
                # No active account — all features degrade to public-only (Layer A)
                for fid in fids:
                    results.append({
                        "vendor_id": vid,
                        "feature_id": fid,
                        "explored": False,
                        "screenshots": [],
                        "api_captures": [],
                        "page_title": "",
                        "page_text_snippet": "",
                        "explored_at": explored_at,
                        "degraded": True,
                        "error": f"no active account for {vid} (status={account_status!r})",
                    })
                continue

            for fid in fids:
                result = self.explore_feature_with_opencli(vid, fid)
                results.append(result)

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _strip_html(self, value: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", value or "")
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    # ------------------------------------------------------------------
    # S4 — Feature comparison matrix synthesis (Layer C)
    # ------------------------------------------------------------------

    def build_feature_matrix(self, feature_id: str) -> dict[str, Any]:
        """Synthesize a standardized feature comparison matrix from KB evidence + opencli captures.

        For each vendor in the registry:
        1. Query KB cache for evidence (_search_kb_cache)
        2. Check opencli captures (data_cache/competitor_validation/captures/<vendor>/<feature>/meta.json)
        3. Synthesize: evidence_count, support_status, screenshots, api_endpoints
        4. Include 'our_product' section (hardcoded 'full' with KB-derived advantages/gaps)
        5. Persist result to data_cache/competitor_validation/feature_matrix/<feature_id>.json
        Returns the matrix dict.
        """
        import json as _json
        from datetime import date
        from pathlib import Path

        root = Path.cwd()
        today = date.today().isoformat()

        # Load feature name from taxonomy
        try:
            from competitor_account_manager import CompetitorAccountManager
            mgr = CompetitorAccountManager()
            taxonomy = mgr.get_feature_taxonomy()
            feature_name = next(
                (f.get("name", feature_id) for f in taxonomy if f.get("id") == feature_id),
                feature_id,
            )
        except Exception:
            feature_name = feature_id

        vendor_ids = ["sap", "kingdee", "weaver", "seeyon"]
        vendors: dict[str, Any] = {}

        for vendor_id in vendor_ids:
            # 1. KB cache evidence
            kb_evidence = self._search_kb_cache(vendor_id, feature_id)

            # 2. opencli captures
            capture_meta_path = (
                root / "data_cache" / "competitor_validation" / "captures"
                / vendor_id / feature_id / "meta.json"
            )
            screenshots: list[str] = []
            last_verified: str = ""
            if capture_meta_path.exists():
                try:
                    meta = _json.loads(capture_meta_path.read_text(encoding="utf-8"))
                    last_verified = meta.get("explored_at", "")
                    screenshot_candidate = (
                        root / "data_cache" / "competitor_validation" / "captures"
                        / vendor_id / feature_id / "page_01.png"
                    )
                    if screenshot_candidate.exists():
                        screenshots.append(
                            f"captures/{vendor_id}/{feature_id}/page_01.png"
                        )
                except Exception:
                    pass

            # 3. API endpoints from captures
            api_path = (
                root / "data_cache" / "competitor_validation" / "api_captures"
                / vendor_id / f"{feature_id}.json"
            )
            api_endpoints: list[str] = []
            if api_path.exists():
                try:
                    api_data = _json.loads(api_path.read_text(encoding="utf-8"))
                    api_endpoints = [
                        r.get("url", "")
                        for r in (api_data.get("requests") or [])[:5]
                        if r.get("url")
                    ]
                except Exception:
                    pass

            # 4. Determine support_status from evidence content keywords
            evidence_count = len(kb_evidence)
            all_text = " ".join(
                (ev.get("content_snippet", "") or ev.get("title", ""))
                for ev in kb_evidence
            ).lower()

            if evidence_count == 0 and not screenshots:
                support_status = "unknown"
                confidence = "low"
            elif any(kw in all_text for kw in ["不支持", "不具备", "暂不", "not support", "unsupported"]):
                support_status = "none"
                confidence = "medium"
            elif any(kw in all_text for kw in ["部分", "基础", "limited", "partial", "仅支持", "简单"]):
                support_status = "partial"
                confidence = "medium" if evidence_count >= 2 else "low"
            elif evidence_count >= 2 or screenshots:
                support_status = "full"
                confidence = "high" if evidence_count >= 3 else "medium"
            else:
                support_status = "unknown"
                confidence = "low"

            # Key differences: summarise first evidence snippet
            key_differences = ""
            for ev in kb_evidence[:1]:
                snippet = ev.get("content_snippet", "") or ev.get("title", "")
                if snippet:
                    key_differences = snippet[:200]

            # help_url from registry if available
            help_url = ""
            try:
                vendor_info = mgr.get_all_vendors()
                for v in vendor_info:
                    if v.get("id") == vendor_id:
                        for fm in v.get("feature_modules", []):
                            if fm.get("id") == feature_id:
                                help_url = fm.get("help_url", "")
            except Exception:
                pass

            vendors[vendor_id] = {
                "support_status": support_status,
                "evidence_count": evidence_count,
                "key_differences": key_differences,
                "screenshots": screenshots,
                "api_endpoints": api_endpoints,
                "help_url": help_url,
                "last_verified": last_verified,
                "confidence": confidence,
            }

        # 5. our_product section — hardcoded full, advantages/gaps from KB if available
        our_advantages: list[str] = []
        our_gaps: list[str] = []
        try:
            # Scan KB for any "our_product" notes tagged to this feature
            own_evidence = self._search_kb_cache("our_product", feature_id)
            for ev in own_evidence[:3]:
                snippet = ev.get("content_snippet", "")
                if "优势" in snippet or "advantage" in snippet.lower():
                    our_advantages.append(snippet[:100])
                elif "差距" in snippet or "gap" in snippet.lower():
                    our_gaps.append(snippet[:100])
        except Exception:
            pass

        matrix: dict[str, Any] = {
            "feature_id": feature_id,
            "feature_name": feature_name,
            "last_updated": today,
            "vendors": vendors,
            "our_product": {
                "support_status": "full",
                "key_advantages": our_advantages,
                "key_gaps": our_gaps,
            },
        }

        # Persist to feature_matrix/<feature_id>.json
        matrix_dir = root / "data_cache" / "competitor_validation" / "feature_matrix"
        matrix_dir.mkdir(parents=True, exist_ok=True)
        try:
            (matrix_dir / f"{feature_id}.json").write_text(
                _json.dumps(matrix, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        return matrix

    def build_all_matrices(self) -> list[dict[str, Any]]:
        """Build matrices for all features in taxonomy. For weekly batch."""
        from competitor_account_manager import CompetitorAccountManager
        mgr = CompetitorAccountManager()
        matrices: list[dict[str, Any]] = []
        for feat in mgr.get_feature_taxonomy():
            matrix = self.build_feature_matrix(feat["id"])
            matrices.append(matrix)
        return matrices
