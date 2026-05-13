from typing import Dict, List, Optional

import pandas as pd


def get_requirements_in_pool(
    df: Optional[pd.DataFrame] = None,
    tickets: Optional[List[Dict]] = None,
    ticket_keys: Optional[List[str]] = None,
    req_pool_service=None,
) -> Dict:
    """
    获取纳入需求库的工单信息。
    优先从原始数据筛选“自定义字段(回复方式)”包含“纳入需求库”，否则回退需求池匹配。
    """
    req_tickets = []
    source_method = ""

    if df is not None and not df.empty:
        reply_col = "自定义字段(回复方式)"
        if reply_col in df.columns:
            req_df = df[df[reply_col].astype(str).str.contains("纳入需求库", na=False)]
            for _, row in req_df.iterrows():
                req_tickets.append(
                    {
                        "问题关键字": str(row.get("问题关键字", "")),
                        "概要": str(row.get("概要", ""))[:100],
                        "经办人": str(row.get("经办人", "")),
                        "创建日期": str(row.get("创建日期", ""))[:10],
                        "回复方式": str(row.get(reply_col, "")),
                        "项目名称": str(row.get("项目名称", "")),
                        "自定义字段(研发确认问题类型)": str(row.get("自定义字段(研发确认问题类型)", "")),
                    }
                )
            source_method = "CSV筛选"

    elif tickets:
        for ticket in tickets:
            reply = str(ticket.get("自定义字段(回复方式)", ""))
            if "纳入需求库" in reply:
                req_tickets.append(
                    {
                        "问题关键字": str(ticket.get("问题关键字", "")),
                        "概要": str(ticket.get("概要", ""))[:100],
                        "经办人": str(ticket.get("经办人", "")),
                        "创建日期": str(ticket.get("创建日期", ""))[:10],
                        "回复方式": reply,
                        "项目名称": str(ticket.get("项目名称", "")),
                        "自定义字段(研发确认问题类型)": str(
                            ticket.get("自定义字段(研发确认问题类型)", "") or ticket.get("研发确认问题类型", "")
                        ),
                    }
                )
        source_method = "周报聚合筛选"

    if not req_tickets and ticket_keys and req_pool_service:
        try:
            all_reqs = req_pool_service.get_all_requirements()
            for req in all_reqs:
                source_issues = req.get("source_issues", [])
                if any(issue in ticket_keys for issue in source_issues):
                    req_tickets.append(
                        {
                            "问题关键字": ", ".join(source_issues[:2]) if source_issues else "-",
                            "概要": str(req.get("title", ""))[:100],
                            "经办人": "-",
                            "创建日期": str(req.get("created_at", ""))[:10],
                            "回复方式": "纳入需求库(需求池)",
                            "项目名称": "-",
                            "自定义字段(研发确认问题类型)": "-",
                            "status": req.get("status", "new"),
                            "req_id": req.get("id", ""),
                        }
                    )
            source_method = "Chroma需求池匹配"
        except Exception as exc:
            print(f"[ReportRequirementInsights] 需求池匹配失败: {exc}")

    return {
        "total_count": len(req_tickets),
        "requirements": req_tickets,
        "source_method": source_method,
    }


def get_process_labeled_issues(
    df: Optional[pd.DataFrame] = None,
    tickets: Optional[List[Dict]] = None,
    label_pattern: str = "流程-",
) -> List[Dict]:
    """
    获取带“流程-”标签的重点关注问题清单。
    """
    labeled_tickets = []

    if df is not None and not df.empty:
        label_col = "标签"
        if label_col in df.columns:
            labeled_df = df[df[label_col].astype(str).str.contains(label_pattern, na=False)]
            for _, row in labeled_df.iterrows():
                labeled_tickets.append(
                    {
                        "问题关键字": str(row.get("问题关键字", "")),
                        "概要": str(row.get("概要", ""))[:100],
                        "经办人": str(row.get("经办人", "")),
                        "创建日期": str(row.get("创建日期", ""))[:10],
                        "标签": str(row.get(label_col, "")),
                        "matched_label": str(row.get(label_col, "")),
                        "项目名称": str(row.get("项目名称", "")),
                        "自定义字段(研发确认问题类型)": str(row.get("自定义字段(研发确认问题类型)", "")),
                    }
                )
    elif tickets:
        for ticket in tickets:
            label = str(ticket.get("标签", "") or ticket.get("labels", ""))
            if label and label_pattern in label:
                labeled_tickets.append(
                    {
                        "问题关键字": str(ticket.get("问题关键字", "")),
                        "概要": str(ticket.get("概要", ""))[:100],
                        "经办人": str(ticket.get("经办人", "")),
                        "创建日期": str(ticket.get("创建日期", ""))[:10],
                        "标签": label,
                        "matched_label": label,
                        "项目名称": str(ticket.get("项目名称", "")),
                        "自定义字段(研发确认问题类型)": str(
                            ticket.get("自定义字段(研发确认问题类型)", "") or ticket.get("研发确认问题类型", "")
                        ),
                    }
                )

    return labeled_tickets
