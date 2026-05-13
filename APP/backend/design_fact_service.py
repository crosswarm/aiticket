from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent


class DesignFactService:
    def __init__(
        self,
        kb_runtime_service: Any | None = None,
        kb_root: str | None = None,
        runtime_dossier_dir: str | None = None,
    ) -> None:
        self.kb_runtime_service = kb_runtime_service
        self.kb_root = Path(kb_root or (PROJECT_ROOT / "KB")).resolve()
        self.profile_dir = self.kb_root / "REQ_POOL" / "product_design_profiles"
        self.kb_dossier_dir = self.kb_root / "REQ_POOL" / "competitor_dossiers"
        self.runtime_dossier_dir = Path(runtime_dossier_dir or (BASE_DIR / "data_cache" / "competitor_dossiers")).resolve()
        self.runtime_dossier_dir.mkdir(parents=True, exist_ok=True)

    def normalize_fact_packet(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(payload or {})

        def _text(key: str) -> str:
            return str(payload.get(key) or "").strip()

        def _list(key: str) -> list[str]:
            value = payload.get(key) or []
            if isinstance(value, str):
                candidates = re.split(r"[,\n;；、]+", value)
            else:
                candidates = value
            normalized: list[str] = []
            for item in candidates:
                cleaned = str(item or "").strip()
                if cleaned and cleaned not in normalized:
                    normalized.append(cleaned)
            return normalized

        return {
            "surface": _text("surface"),
            "trigger_action": _text("trigger_action"),
            "input_object": _text("input_object"),
            "requiredness": _text("requiredness"),
            "visibility_scope": _text("visibility_scope"),
            "persistence_scope": _text("persistence_scope"),
            "config_level": _list("config_level"),
            "related_process_types": _list("related_process_types"),
            "related_bill_types": _list("related_bill_types"),
            "known_constraints": _list("known_constraints"),
            "manual_notes": _text("manual_notes"),
            "reference_links": _list("reference_links"),
            "attachments": _list("attachments"),
            "updated_at": _text("updated_at") or datetime.now().isoformat(),
        }

    def build_requirement_context(
        self,
        requirement: dict[str, Any],
        evidence_bundle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requirement = requirement or {}
        fact_packet = self.normalize_fact_packet(requirement.get("requirement_fact_packet") or {})
        focus = self._build_focus(requirement, fact_packet, evidence_bundle or {})
        matched_profiles = self._match_profiles(focus)
        kb_fact_items = self._search_kb_fact_items(focus)
        design_fact_bundle = self._build_design_fact_bundle(focus, matched_profiles, kb_fact_items, fact_packet)
        competitor_dossiers = self._match_competitor_dossiers(focus)
        return {
            "fact_packet": fact_packet,
            "design_fact_bundle": design_fact_bundle,
            "competitor_dossiers": competitor_dossiers,
        }

    def save_competitor_dossier(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_competitor_dossier(payload)
        filename = f"{self._slugify(normalized['vendor'])}-{self._slugify(normalized['feature_key'])}-{normalized['dossier_id']}.json"
        path = self.runtime_dossier_dir / filename
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return normalized

    def _normalize_competitor_dossier(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(payload or {})

        def _text(key: str) -> str:
            return str(payload.get(key) or "").strip()

        def _list(key: str) -> list[Any]:
            value = payload.get(key) or []
            if isinstance(value, str):
                return [item.strip() for item in re.split(r"[,\n;；、]+", value) if item.strip()]
            return value

        evidence_items = []
        for item in _list("evidence_items"):
            if isinstance(item, dict):
                evidence_items.append(
                    {
                        "title": str(item.get("title") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                        "source_level": str(item.get("source_level") or "internal_dossier").strip() or "internal_dossier",
                        "snippet": str(item.get("snippet") or "").strip(),
                    }
                )
            else:
                cleaned = str(item or "").strip()
                if cleaned:
                    evidence_items.append({"title": cleaned, "url": "", "source_level": "internal_dossier", "snippet": ""})

        captures = []
        for item in _list("captures"):
            if isinstance(item, dict):
                captures.append(
                    {
                        "title": str(item.get("title") or "").strip(),
                        "file_path": str(item.get("file_path") or "").strip(),
                    }
                )
            else:
                cleaned = str(item or "").strip()
                if cleaned:
                    captures.append({"title": cleaned, "file_path": ""})

        vendor = _text("vendor")
        feature_key = _text("feature_key")
        if not vendor or not feature_key:
            raise ValueError("vendor and feature_key are required")

        return {
            "dossier_id": _text("dossier_id") or self._slugify(f"{vendor}-{feature_key}-{datetime.now().isoformat()}"),
            "vendor": vendor,
            "feature_key": feature_key,
            "feature_summary": _text("feature_summary"),
            "supported": bool(payload.get("supported", False)),
            "ui_touchpoints": [str(item).strip() for item in _list("ui_touchpoints") if str(item).strip()],
            "config_levels": [str(item).strip() for item in _list("config_levels") if str(item).strip()],
            "constraints": [str(item).strip() for item in _list("constraints") if str(item).strip()],
            "evidence_items": evidence_items,
            "verification_status": _text("verification_status") or "unverified",
            "captures": captures,
            "notes": _text("notes"),
            "aliases": [str(item).strip() for item in _list("aliases") if str(item).strip()],
            "updated_at": _text("updated_at") or datetime.now().isoformat(),
        }

    def _build_focus(
        self,
        requirement: dict[str, Any],
        fact_packet: dict[str, Any],
        evidence_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        title = str(requirement.get("title") or "")
        description = str(requirement.get("description") or "")
        ai_analysis = requirement.get("ai_analysis", {}) or {}
        topic_names = list(evidence_bundle.get("topic_names", []) or ai_analysis.get("topic_names", []) or [])
        fields = [
            title,
            description,
            fact_packet.get("surface", ""),
            fact_packet.get("trigger_action", ""),
            fact_packet.get("input_object", ""),
            fact_packet.get("manual_notes", ""),
            " ".join(topic_names),
        ]
        tokens = []
        for field in fields:
            for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", str(field or "")):
                if token not in tokens:
                    tokens.append(token)
        summary = " / ".join(item for item in [fact_packet.get("surface"), fact_packet.get("trigger_action"), fact_packet.get("input_object")] if item) or title
        return {
            "title": title,
            "description": description,
            "summary": summary,
            "surface": fact_packet.get("surface") or self._pick_first_matching(fields, ["审批面板", "流程监控", "流程图", "重提弹窗", "待办"]),
            "trigger_action": fact_packet.get("trigger_action") or self._pick_first_matching(fields, ["重新提交", "重提", "提交", "驳回后提交"]),
            "input_object": fact_packet.get("input_object") or self._pick_first_matching(fields, ["附言说明", "补充说明", "说明", "备注", "审批意见"]),
            "terms": tokens[:16],
            "topic_names": topic_names,
        }

    def _match_profiles(self, focus: dict[str, Any]) -> list[dict[str, Any]]:
        profiles = []
        for path in sorted(self.profile_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            text = " ".join(
                [
                    str(payload.get("feature_key") or ""),
                    str(payload.get("title") or ""),
                    str(payload.get("module") or ""),
                    " ".join(payload.get("aliases", []) or []),
                ]
            ).lower()
            score = sum(1 for term in focus.get("terms", [])[:10] if term.lower() in text)
            if focus.get("surface") and focus["surface"].lower() in text:
                score += 2
            if focus.get("input_object") and focus["input_object"].lower() in text:
                score += 2
            if score <= 0:
                continue
            profiles.append({**payload, "_match_score": score, "_source_path": str(path)})
        profiles.sort(key=lambda item: item.get("_match_score", 0), reverse=True)
        return profiles[:4]

    def _search_kb_fact_items(self, focus: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.kb_runtime_service:
            return []
        queries = list(
            dict.fromkeys(
                [
                    " ".join(item for item in [focus.get("surface"), focus.get("trigger_action"), focus.get("input_object")] if item),
                    " ".join(item for item in [focus.get("surface"), focus.get("input_object"), "配置 参数"] if item),
                    " ".join(item for item in [focus.get("surface"), focus.get("input_object"), "展示 留痕"] if item),
                    " ".join(item for item in [focus.get("trigger_action"), focus.get("input_object"), "透传"] if item),
                    " ".join(item for item in ["流程属性", focus.get("input_object") or focus.get("surface") or focus.get("summary")]),
                    " ".join(item for item in ["租户 参数", focus.get("surface") or focus.get("summary")]),
                ]
            )
        )
        merged: list[dict[str, Any]] = []
        seen = set()
        for query in queries:
            if not query.strip():
                continue
            try:
                bundle = self.kb_runtime_service.search_bundle(query, top_k=6)
            except Exception:
                continue
            for item in bundle.get("items", []) or []:
                identity = item.get("citation_label") or item.get("content_id") or item.get("name")
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                merged.append(item)
        return merged[:12]

    def _build_design_fact_bundle(
        self,
        focus: dict[str, Any],
        matched_profiles: list[dict[str, Any]],
        kb_fact_items: list[dict[str, Any]],
        fact_packet: dict[str, Any],
    ) -> dict[str, Any]:
        bundle = {
            "design_principles": [],
            "process_rules": [],
            "step_rules": [],
            "tenant_params": [],
            "document_properties": [],
            "source_refs": [],
            "matched_profiles": [
                {
                    "feature_key": item.get("feature_key", ""),
                    "title": item.get("title", ""),
                    "module": item.get("module", ""),
                }
                for item in matched_profiles
            ],
        }

        for profile in matched_profiles:
            bundle["source_refs"].extend(profile.get("source_refs", []) or [])
            for principle in profile.get("design_principles", []) or []:
                bundle["design_principles"].append({"name": profile.get("title", "设计原则"), "summary": str(principle), "source": "product_profile"})
            for key, target in (
                ("process_level_rules", "process_rules"),
                ("step_level_rules", "step_rules"),
                ("tenant_level_params", "tenant_params"),
                ("document_flow_properties", "document_properties"),
            ):
                for item in profile.get(key, []) or []:
                    bundle[target].append(
                        {
                            "name": str(item.get("name") or profile.get("title") or "未命名规则"),
                            "summary": str(item.get("summary") or ""),
                            "source": "product_profile",
                            "citation_label": f"[PROFILE] {profile.get('feature_key', '')}".strip(),
                        }
                    )

        for item in kb_fact_items:
            category = self._categorize_kb_item(item)
            target = bundle.get(category)
            if target is None:
                continue
            target.append(
                {
                    "name": item.get("name", "未命名资料"),
                    "summary": item.get("summary", ""),
                    "source": item.get("source_kind", "kb_local"),
                    "citation_label": item.get("citation_label", ""),
                }
            )
            if item.get("citation_label"):
                bundle["source_refs"].append(item["citation_label"])

        for category, entry in self._manual_fact_entries(fact_packet):
            bundle[category].append(entry)

        for key in ("design_principles", "process_rules", "step_rules", "tenant_params", "document_properties"):
            bundle[key] = self._dedupe_fact_entries(bundle[key])

        coverage_summary = {
            "design_principle_count": len(bundle["design_principles"]),
            "process_rule_count": len(bundle["process_rules"]),
            "step_rule_count": len(bundle["step_rules"]),
            "tenant_param_count": len(bundle["tenant_params"]),
            "document_property_count": len(bundle["document_properties"]),
            "matched_profile_count": len(matched_profiles),
            "matched_kb_fact_count": len(kb_fact_items),
            "manual_fact_count": sum(1 for item in self._manual_fact_entries(fact_packet)),
        }
        coverage_summary["total_fact_count"] = (
            coverage_summary["design_principle_count"]
            + coverage_summary["process_rule_count"]
            + coverage_summary["step_rule_count"]
            + coverage_summary["tenant_param_count"]
            + coverage_summary["document_property_count"]
        )

        missing_facts = self._build_missing_facts(focus, fact_packet, bundle)
        coverage_summary["missing_fact_count"] = len(missing_facts)

        bundle["coverage_summary"] = coverage_summary
        bundle["missing_facts"] = missing_facts
        bundle["source_refs"] = list(dict.fromkeys([item for item in bundle["source_refs"] if item]))[:20]
        bundle["focus_summary"] = focus.get("summary", "")
        return bundle

    def _categorize_kb_item(self, item: dict[str, Any]) -> str:
        haystack = " ".join(
            [
                str(item.get("name") or ""),
                str(item.get("summary") or ""),
                str(item.get("source_rel_path") or ""),
            ]
        )
        if any(term in haystack for term in ["参数", "租户", "开关"]):
            return "tenant_params"
        if any(term in haystack for term in ["状态对照", "流程属性", "单据", "业务阶段"]):
            return "document_properties"
        if any(term in haystack for term in ["审批面板", "待办打开单据", "当前审批人", "特殊场景", "提交", "撤回", "重提", "展示"]):
            return "step_rules"
        if any(term in haystack for term in ["流程定义", "流程管理", "规则活动", "规则引擎", "发起流程", "流程运转"]):
            return "process_rules"
        return "design_principles"

    def _manual_fact_entries(self, fact_packet: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        entries: list[tuple[str, dict[str, Any]]] = []
        summary = " / ".join(item for item in [fact_packet.get("surface"), fact_packet.get("trigger_action"), fact_packet.get("input_object")] if item)
        if summary or fact_packet.get("requiredness"):
            entries.append(
                (
                    "step_rules",
                    {
                        "name": summary or "需求级人工补充场景",
                        "summary": "；".join(item for item in [fact_packet.get("requiredness"), fact_packet.get("visibility_scope"), fact_packet.get("persistence_scope")] if item),
                        "source": "requirement_manual",
                        "citation_label": "[MANUAL] 当前需求事实补充",
                    },
                )
            )
        for level in fact_packet.get("config_level", []) or []:
            if "租户" in level:
                entries.append(("tenant_params", {"name": f"{level}配置", "summary": fact_packet.get("manual_notes", ""), "source": "requirement_manual", "citation_label": "[MANUAL] 当前需求事实补充"}))
            elif "单据属性" in level:
                entries.append(("document_properties", {"name": f"{level}配置", "summary": "；".join(fact_packet.get("related_bill_types", []) or []) or fact_packet.get("manual_notes", ""), "source": "requirement_manual", "citation_label": "[MANUAL] 当前需求事实补充"}))
            elif "流程" in level:
                entries.append(("process_rules", {"name": f"{level}配置", "summary": "；".join(fact_packet.get("related_process_types", []) or []) or fact_packet.get("manual_notes", ""), "source": "requirement_manual", "citation_label": "[MANUAL] 当前需求事实补充"}))
            else:
                entries.append(("step_rules", {"name": f"{level}配置", "summary": fact_packet.get("manual_notes", ""), "source": "requirement_manual", "citation_label": "[MANUAL] 当前需求事实补充"}))
        if fact_packet.get("related_bill_types"):
            entries.append(("document_properties", {"name": "关联单据类型", "summary": "、".join(fact_packet["related_bill_types"]), "source": "requirement_manual", "citation_label": "[MANUAL] 当前需求事实补充"}))
        if fact_packet.get("known_constraints"):
            entries.append(("design_principles", {"name": "已知约束", "summary": "；".join(fact_packet["known_constraints"]), "source": "requirement_manual", "citation_label": "[MANUAL] 当前需求事实补充"}))
        return entries

    def _build_missing_facts(
        self,
        focus: dict[str, Any],
        fact_packet: dict[str, Any],
        bundle: dict[str, Any],
    ) -> list[str]:
        missing = []
        if not bundle["process_rules"]:
            missing.append("流程级配置仍不明确，需补充该能力是否受流程定义或流程类型控制。")
        if not bundle["step_rules"]:
            missing.append("环节级规则仍不明确，需确认页面/节点上的触发、展示和校验规则。")
        if not bundle["tenant_params"]:
            missing.append("租户级参数仍不明确，需确认是否存在开关、默认值或灰度控制。")
        if not bundle["document_properties"]:
            missing.append("单据流程属性仍不明确，需确认不同单据类型和流程属性的差异。")
        if not fact_packet.get("requiredness"):
            missing.append("附言/说明是否必填仍不明确。")
        if not fact_packet.get("visibility_scope"):
            missing.append("附言/说明的可见范围仍不明确。")
        if not fact_packet.get("persistence_scope"):
            missing.append("附言/说明的留痕与透传规则仍不明确。")
        if not focus.get("surface"):
            missing.append("当前需求落在哪个页面或环节仍不明确。")
        return list(dict.fromkeys(missing))[:8]

    def _match_competitor_dossiers(self, focus: dict[str, Any]) -> list[dict[str, Any]]:
        dossiers = []
        for directory in (self.kb_dossier_dir, self.runtime_dossier_dir):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    payload = self._normalize_competitor_dossier(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                text = " ".join(
                    [
                        payload.get("feature_key", ""),
                        payload.get("feature_summary", ""),
                        " ".join(payload.get("aliases", []) or []),
                        " ".join(payload.get("ui_touchpoints", []) or []),
                    ]
                ).lower()
                score = sum(1 for term in focus.get("terms", [])[:10] if term.lower() in text)
                if focus.get("surface") and focus["surface"].lower() in text:
                    score += 2
                if focus.get("input_object") and focus["input_object"].lower() in text:
                    score += 2
                if score <= 0:
                    continue
                dossiers.append({**payload, "_match_score": score})
        dossiers.sort(key=lambda item: item.get("_match_score", 0), reverse=True)
        return dossiers[:6]

    def _pick_first_matching(self, fields: list[str], terms: list[str]) -> str:
        text = "\n".join(str(item or "") for item in fields)
        for term in terms:
            if term in text:
                return term
        return ""

    def _dedupe_fact_entries(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped = []
        seen = set()
        for item in items:
            key = (
                str(item.get("name") or "").strip(),
                str(item.get("summary") or "").strip(),
                str(item.get("source") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:12]

    def _slugify(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip())
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-").lower()
        return cleaned or "item"
