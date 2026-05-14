#!/usr/bin/env python3
"""
用 JiraService（后端凭据）直接导出周报格式 CSV
绕开 jira_query.py 的机器绑定加密，可在 Mini/QCL 任意机器运行

用法:
  python export_weekly_csv.py            # 导出上周数据（默认）
  python export_weekly_csv.py --this     # 导出本周数据
  python export_weekly_csv.py --week-offset -2  # 导出两周前
"""
import csv
import hashlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

# 设置路径
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
SRC_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BACKEND_DIR))

# strict 模式（QCL/deployable）禁用周报脚本；改为前端在线触发
try:
    from role_guard import is_strict_role
    if is_strict_role():
        print("[export_weekly_csv] strict 模式（QCL/deployable），周报脚本已禁用，直接退出")
        sys.exit(0)
except ImportError:
    pass

from jira_service import JiraService
from analysis import PROJECT_DISPLAY_NAMES


# ─── 字段提取工具（与 jira_query.py 保持一致） ────────────────────────────────

def _extract_cascading(issue, field_id):
    cf = issue.get("fields", {}).get(field_id)
    if not cf:
        return ""
    if isinstance(cf, dict):
        parent = cf.get("value", "")
        child = cf.get("child", {}).get("value", "") if isinstance(cf.get("child"), dict) else ""
        return f"{parent} -> {child}" if child else parent
    return str(cf)


def _extract_list(issue, field_id):
    cf = issue.get("fields", {}).get(field_id)
    if isinstance(cf, list) and cf:
        return cf[0].rstrip(",").strip() if isinstance(cf[0], str) else str(cf[0])
    return ""


def _extract_user(issue, field_id):
    cf = issue.get("fields", {}).get(field_id)
    if isinstance(cf, dict):
        return cf.get("displayName", cf.get("name", ""))
    return str(cf) if cf else ""


def _fmt_datetime(dt_str):
    if not dt_str:
        return ""
    return dt_str[:16].replace("T", " ")


def _extract(issue, path, default=""):
    """点分路径提取"""
    parts = path.split(".")
    val = issue
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p, default)
        else:
            return default
    return val if val is not None else default


# 周报 CSV 列定义（与 jira_query.py REPORT_CSV_COLUMNS 完全一致）
REPORT_CSV_COLUMNS = [
    ("问题关键字",                lambda i: i.get("key", "")),
    ("问题ID",                    lambda i: i.get("id", "")),
    ("项目关键字",                lambda i: _extract(i, "fields.project.key")),
    ("项目名称",                  lambda i: _extract(i, "fields.project.name")),
    ("项目类型",                  lambda i: _extract(i, "fields.project.projectTypeKey")),
    ("项目主管",                  lambda i: ""),
    ("项目描述",                  lambda i: ""),
    ("项目URL",                   lambda i: ""),
    ("自定义字段(领域模块)",      lambda i: _extract_cascading(i, "customfield_10123")),
    ("经办人",                    lambda i: _extract(i, "fields.assignee.name")),
    ("概要",                      lambda i: _extract(i, "fields.summary")),
    ("创建日期",                  lambda i: _fmt_datetime(_extract(i, "fields.created", ""))),
    ("状态",                      lambda i: _extract(i, "fields.status.name")),
    ("自定义字段(到期日)",        lambda i: _fmt_datetime(_extract(i, "fields.duedate", ""))),
    ("创建者",                    lambda i: _extract(i, "fields.reporter.name")),
    ("自定义字段(项目名称)",      lambda i: _extract_list(i, "customfield_10725")),
    ("自定义字段(SOP产品版本)",   lambda i: _extract(i, "fields.customfield_13529.value")),
    ("自定义字段(客户问题类型)",  lambda i: _extract(i, "fields.customfield_10402.value")),
    ("自定义字段(研发确认问题类型)", lambda i: _extract(i, "fields.customfield_10729.value")),
    ("自定义字段(客户属性)",      lambda i: _extract(i, "fields.customfield_13211.value")),
    ("自定义字段(回复方式)",      lambda i: _extract(i, "fields.customfield_10410.value")),
    ("自定义字段(重点客户类型)",  lambda i: _extract(i, "fields.customfield_14301.value")),
    ("自定义字段(解决方案)",      lambda i: _extract(i, "fields.customfield_10411")),
    ("自定义字段(所属伙伴)",      lambda i: _extract(i, "fields.customfield_11910")),
    ("自定义字段(所属大区)",      lambda i: _extract(i, "fields.customfield_11908")),
    ("自定义字段(机构)",          lambda i: _extract(i, "fields.customfield_11909")),
    ("自定义字段(解决方式)",      lambda i: _extract(i, "fields.customfield_10906")),
    ("自定义字段(需求负责人)",    lambda i: _extract_user(i, "customfield_10401")),
    ("标签",                      lambda i: ", ".join(i.get("fields", {}).get("labels", []))),
]

# 需要请求的自定义字段 ID
CUSTOM_FIELDS = ",".join([
    "summary", "project", "assignee", "reporter", "status", "created", "duedate", "labels",
    "customfield_10123",  # 领域模块
    "customfield_10725",  # 项目名称（客户）
    "customfield_13529",  # SOP产品版本
    "customfield_10402",  # 客户问题类型
    "customfield_10729",  # 研发确认问题类型
    "customfield_13211",  # 客户属性
    "customfield_10410",  # 回复方式
    "customfield_14301",  # 重点客户类型
    "customfield_10411",  # 解决方案
    "customfield_11910",  # 所属伙伴
    "customfield_11908",  # 所属大区
    "customfield_11909",  # 机构
    "customfield_10906",  # 解决方式
    "customfield_10401",  # 需求负责人
    "customfield_11935",  # 初始项目（转出判断用）
])


def get_week_range(offset=0):
    """获取周范围 (周一 ~ 周日)，offset=-1 表示上周"""
    today = datetime.now()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def fetch_all_issues(jira: JiraService, jql: str) -> list:
    """分页获取所有 issue"""
    issues = []
    start_at = 0
    page_size = 100

    while True:
        result = jira.search_issues_rest_api(jql, start_at=start_at, max_results=page_size, fields=CUSTOM_FIELDS)
        batch = result.get("issues", [])
        if not batch:
            break
        issues.extend(batch)
        total = result.get("total", 0)
        start_at += len(batch)
        print(f"  已获取 {start_at}/{total} 条...", end="\r")
        if start_at >= total:
            break

    print(f"  共获取 {len(issues)} 条                ")
    return issues


def _modules_slug(domain_modules: Optional[List[str]]) -> str:
    """返回 domain_modules 的 6 位 md5 slug（空=无 slug）"""
    if not domain_modules:
        return ""
    return "_" + hashlib.md5(",".join(sorted(domain_modules)).encode()).hexdigest()[:6]


def export_weekly_csv(week_offset: int = -1, project_key: str = "MYPROJECT",
                      domain_modules: Optional[List[str]] = None) -> Path:
    """导出周数据到 CSV，返回输出文件路径"""
    start, end = get_week_range(week_offset)
    label = "上周" if week_offset == -1 else "本周" if week_offset == 0 else f"偏移{week_offset}周"
    print(f"\n📊 导出{project_key} {label}数据 ({start} ~ {end})")

    project_name = PROJECT_DISPLAY_NAMES.get(project_key, project_key)
    slug = _modules_slug(domain_modules)
    ts = datetime.now().strftime("%Y-%m-%dT%H_%M_%S+0800")
    filename = f"{project_key}{slug}-周数据-{start}-{end}T{ts.split('T')[1]}.csv"
    output = SRC_DIR / filename

    jira = JiraService()
    all_issues = []

    # 领域模块过滤（cf[10123]）
    modules_filter = ""
    if domain_modules:
        quoted = ", ".join(f'"{m}"' for m in domain_modules)
        modules_filter = f' AND cf[10123] in ({quoted})'

    # 1. 核心工单
    jql_core = (
        f'project = {project_key} AND issuetype = "支持问题" '
        f'AND created >= "{start}" AND created < "{end}"'
        f'{modules_filter}'
    )
    print(f"\n[1/2] {project_key} 核心工单...")
    core_issues = fetch_all_issues(jira, jql_core)
    all_issues.extend(core_issues)

    # 2. 转出工单（cf[11935] = 本项目中文名 AND project != 本项目）
    jql_trans = (
        f'issuetype = "支持问题" AND cf[11935] = "{project_name}" '
        f'AND project != {project_key} '
        f'AND created >= "{start}" AND created < "{end}"'
        f'{modules_filter}'
    )
    print(f"[2/2] 转出工单...")
    trans_issues = fetch_all_issues(jira, jql_trans)
    all_issues.extend(trans_issues)

    # 去重（按 issue key）
    seen = set()
    unique_issues = []
    for iss in all_issues:
        key = iss.get("key")
        if key not in seen:
            seen.add(key)
            unique_issues.append(iss)

    print(f"\n✅ 合并去重: 核心 {len(core_issues)} + 转出 {len(trans_issues)} → {len(unique_issues)} 条")

    # 写 CSV（utf-8-sig，与手动导出格式一致）
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([col[0] for col in REPORT_CSV_COLUMNS])
        for i in unique_issues:
            w.writerow([col[1](i) for col in REPORT_CSV_COLUMNS])

    print(f"📁 已保存: {output.name}")
    return output


def main():
    import argparse
    parser = argparse.ArgumentParser(description="用 JiraService 导出周报格式 CSV")
    parser.add_argument("--this", action="store_true", help="导出本周（默认上周）")
    parser.add_argument("--week-offset", type=int, default=None, help="周偏移量（负数=过去）")
    parser.add_argument("--project", default="MYPROJECT", help="Jira 项目 key（默认 MYPROJECT）")
    parser.add_argument("--modules", default="", help="逗号分隔领域模块（为空=全部）")
    args = parser.parse_args()

    if args.week_offset is not None:
        offset = args.week_offset
    elif args.this:
        offset = 0
    else:
        offset = -1  # 默认上周

    mods = [m.strip() for m in args.modules.split(",") if m.strip()] if args.modules else None
    output = export_weekly_csv(offset, project_key=args.project, domain_modules=mods)
    print(f"\n🎯 CSV 已就绪: {output}")
    return str(output)


if __name__ == "__main__":
    main()
