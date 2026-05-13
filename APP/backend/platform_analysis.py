"""
应用与开发平台 降工单月报生成器
核心 KPI：每客户问题数（= 工单数 ÷ 去重客户数，字段 customfield_10725）

报告结构：
  一、核心 KPI 速览
  二、平台月度概况（总量同比/环比 + Q1-Q4 趋势）
  三、各项目详细指标（每客户问题数 + 同比环比 + 达标）
  四、风险分析
  五、多维度分析（问题类型 + 解决方式，按需启用）

用法:
  conda run -n antigravity python platform_analysis.py --month 2026-03
  python platform_analysis.py --month 2026-03 --no-detail   # 跳过多维度（快速模式）
"""

import os, sys, json, argparse, ssl, base64, hashlib, socket
from datetime import datetime, date
from calendar import monthrange
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError
from typing import Dict, List, Optional, Tuple, Set

BASE_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "design" / "plans" / "platform-reduction-config.json"

# ── Jira 鉴权 ─────────────────────────────────────────────────────────────────

def _find_tq_config() -> Path:
    for p in [
        PROJECT_ROOT / ".agent" / "skills" / "ticket-query" / "config.json",
        PROJECT_ROOT.parent.parent / ".agent" / "skills" / "ticket-query" / "config.json",
        Path.home() / "Studio" / "aiticket" / ".agent" / "skills" / "ticket-query" / "config.json",
    ]:
        if p.exists():
            return p
    raise FileNotFoundError("ticket-query config 未找到，请先运行 --setup")

def _derive_key() -> bytes:
    mid = f"{socket.gethostname()}:{os.path.expanduser('~')}"
    return base64.urlsafe_b64encode(
        hashlib.pbkdf2_hmac('sha256', mid.encode(), b'ticket-query-skill-v1', 100_000)
    )

def _jira_creds() -> Tuple[str, str, str, bool]:
    cfg = json.loads(_find_tq_config().read_text())
    try:
        from cryptography.fernet import Fernet
        pwd = Fernet(_derive_key()).decrypt(cfg['password_enc'].encode()).decode()
    except Exception as e:
        raise RuntimeError(f"密码解密失败: {e}")
    return cfg['jira_base_url'], cfg['username'], pwd, cfg.get('ssl_verify', True)

_CREDS = None
def _get_creds():
    global _CREDS
    if _CREDS is None:
        _CREDS = _jira_creds()
    return _CREDS

def jira_request(path: str, params: Optional[Dict] = None) -> dict:
    base_url, username, password, ssl_verify = _get_creds()
    url = f"{base_url}{path}"
    if params:
        url += "?" + urlencode(params, quote_via=quote)
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = Request(url, headers={
        "Authorization": f"Basic {cred}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    ctx = ssl.create_default_context()
    if not ssl_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode())

# ── Jira 查询工具 ─────────────────────────────────────────────────────────────

def build_jql(project_key: str, start: str, end: str, issue_type: str) -> str:
    return (f'project = {project_key} AND issuetype = "{issue_type}" '
            f'AND created >= "{start}" AND created <= "{end}"')

def count_issues(project_key: str, start: str, end: str, issue_type: str) -> int:
    """只取 total，不下载明细"""
    d = jira_request('/rest/api/2/search', {
        'jql': build_jql(project_key, start, end, issue_type),
        'maxResults': 0, 'fields': 'summary',
    })
    return d.get('total', 0)

def count_customers_and_issues(project_key: str, start: str, end: str,
                                issue_type: str) -> Tuple[int, int]:
    """返回 (total_issues, unique_customer_count)，分页拉 customfield_10725"""
    customers: Set[str] = set()
    start_at, total = 0, 0
    while True:
        d = jira_request('/rest/api/2/search', {
            'jql': build_jql(project_key, start, end, issue_type),
            'maxResults': 500, 'startAt': start_at,
            'fields': 'customfield_10725',
        })
        total = d.get('total', 0)
        issues = d.get('issues', [])
        for iss in issues:
            vals = iss['fields'].get('customfield_10725') or []
            for v in (vals if isinstance(vals, list) else [vals]):
                if v:
                    customers.add(str(v).strip())
        start_at += len(issues)
        if start_at >= total or not issues:
            break
    return total, len(customers)

def count_cross_dedup_customers(project_keys: List[str], start: str, end: str,
                                 issue_type: str) -> Tuple[int, int, List[Dict]]:
    """跨项目合并查询，返回 (total_issues, 真实去重客户数, 高影响力客户列表)
    同一客户在多个项目都有工单时只计一次（真实 IPC 分母）。
    高影响力客户 = 同时出现在 ≥2 个项目的客户，按总工单量降序 TOP30。
    """
    proj_str = ",".join(project_keys)
    jql = (f'project in ({proj_str}) AND issuetype = "{issue_type}" '
           f'AND created >= "{start}" AND created <= "{end}"')
    # customer → {proj_key: count}
    cust_proj: Dict[str, Dict[str, int]] = {}
    start_at, total = 0, 0
    while True:
        d = jira_request('/rest/api/2/search', {
            'jql': jql, 'maxResults': 500, 'startAt': start_at,
            'fields': 'customfield_10725',
        })
        total = d.get('total', 0)
        issues = d.get('issues', [])
        for iss in issues:
            proj_key = iss['key'].split('-')[0]   # 从工单 Key 提取项目
            vals = iss['fields'].get('customfield_10725') or []
            for v in (vals if isinstance(vals, list) else [vals]):
                if v:
                    cname = str(v).strip()
                    if cname not in cust_proj:
                        cust_proj[cname] = {}
                    cust_proj[cname][proj_key] = cust_proj[cname].get(proj_key, 0) + 1
        start_at += len(issues)
        if start_at >= total or not issues:
            break

    # 高影响力客户：涉及 ≥2 个项目，按总工单降序 TOP30
    hi: List[Dict] = []
    for cname, pd in cust_proj.items():
        proj_cnt = len(pd)
        total_t  = sum(pd.values())
        if proj_cnt >= 2:
            top_projs = sorted(pd.items(), key=lambda x: -x[1])
            hi.append({
                "name": cname,
                "total_tickets": total_t,
                "project_count": proj_cnt,
                "by_project": dict(top_projs),
            })
    hi.sort(key=lambda x: (-x['project_count'], -x['total_tickets']))
    return total, len(cust_proj), hi[:30]

# ── 日期工具 ─────────────────────────────────────────────────────────────────

def month_range(y: int, m: int) -> Tuple[str, str]:
    last = monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"

def ytd_range(y: int, m: int) -> Tuple[str, str]:
    last = monthrange(y, m)[1]
    return f"{y:04d}-01-01", f"{y:04d}-{m:02d}-{last:02d}"

def prev_month(y: int, m: int) -> Tuple[int, int]:
    return (y, m - 1) if m > 1 else (y - 1, 12)

def ipc(issues: int, customers: int) -> Optional[float]:
    return round(issues / customers, 2) if customers else None

def pct_chg(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return round((new - old) / old * 100, 1)

# ── 单项目分析 ────────────────────────────────────────────────────────────────

def analyze_project(proj: Dict, year: int, month: int, issue_type: str,
                    full_detail: bool = False) -> Dict:
    key  = proj['key']
    bl   = proj['baseline_2025']          # 2025 全年工单
    bl_c = proj['baseline_2025_customers'] # 2025 全年去重客户
    bl_ipc = proj['baseline_ipc']         # 2025 每客户问题数
    target_ipc = round(bl_ipc * 0.8, 2)  # 目标：降 20%
    monthly_target = round(bl * 0.8 / 12, 1)

    # ── 本月 ──────────────────────────────────────────────────────
    ms, me = month_range(year, month)
    month_cnt, month_cust = count_customers_and_issues(key, ms, me, issue_type)
    month_ipc = ipc(month_cnt, month_cust)

    # ── 上月 ──────────────────────────────────────────────────────
    py, pm = prev_month(year, month)
    pms, pme = month_range(py, pm)
    prev_cnt, prev_cust = count_customers_and_issues(key, pms, pme, issue_type)
    prev_ipc = ipc(prev_cnt, prev_cust)
    mom_cnt_pct  = pct_chg(month_cnt, prev_cnt)
    mom_ipc_pct  = pct_chg(month_ipc, prev_ipc)

    # ── 去年同月 ──────────────────────────────────────────────────
    yoys, yoye = month_range(year - 1, month)
    yoy_cnt, yoy_cust = count_customers_and_issues(key, yoys, yoye, issue_type)
    yoy_ipc = ipc(yoy_cnt, yoy_cust)
    yoy_cnt_pct = pct_chg(month_cnt, yoy_cnt)
    yoy_ipc_pct = pct_chg(month_ipc, yoy_ipc)

    # ── YTD ───────────────────────────────────────────────────────
    ytds, ytde = ytd_range(year, month)
    ytd_cnt, ytd_cust = count_customers_and_issues(key, ytds, ytde, issue_type)
    ytd_ipc = ipc(ytd_cnt, ytd_cust)

    yytds, yytde = ytd_range(year - 1, month)
    yytd_cnt, yytd_cust = count_customers_and_issues(key, yytds, yytde, issue_type)
    yytd_ipc = ipc(yytd_cnt, yytd_cust)
    ytd_cnt_pct = pct_chg(ytd_cnt, yytd_cnt)
    ytd_ipc_pct = pct_chg(ytd_ipc, yytd_ipc)

    # ── 风险判断（基于 YTD IPC，跨月去重后才能正确衡量客户密度）────────
    on_track_cnt = month_cnt <= monthly_target
    on_track_ipc = (ytd_ipc is not None and ytd_ipc <= target_ipc)
    # ytd_ipc_pct：本年YTD IPC vs 去年同期YTD IPC（趋势方向）
    risk_level = "🔴 高" if (not on_track_ipc and ytd_ipc_pct and ytd_ipc_pct > 10) else \
                 "🟡 中" if not on_track_ipc else "🟢 低"

    return {
        "key": key, "name": proj['name'], "priority": proj['priority'],
        # 基准
        "baseline_2025": bl, "baseline_2025_customers": bl_c, "baseline_ipc": bl_ipc,
        "target_ipc": target_ipc, "monthly_target": monthly_target,
        # 本月
        "month_cnt": month_cnt, "month_cust": month_cust, "month_ipc": month_ipc,
        # 上月
        "prev_cnt": prev_cnt, "prev_cust": prev_cust, "prev_ipc": prev_ipc,
        "mom_cnt_pct": mom_cnt_pct, "mom_ipc_pct": mom_ipc_pct,
        # 去年同月
        "yoy_cnt": yoy_cnt, "yoy_cust": yoy_cust, "yoy_ipc": yoy_ipc,
        "yoy_cnt_pct": yoy_cnt_pct, "yoy_ipc_pct": yoy_ipc_pct,
        # YTD
        "ytd_cnt": ytd_cnt, "ytd_cust": ytd_cust, "ytd_ipc": ytd_ipc,
        "yytd_cnt": yytd_cnt, "yytd_cust": yytd_cust, "yytd_ipc": yytd_ipc,
        "ytd_cnt_pct": ytd_cnt_pct, "ytd_ipc_pct": ytd_ipc_pct,
        # 达标 & 风险
        "on_track_cnt": on_track_cnt, "on_track_ipc": on_track_ipc,
        "risk_level": risk_level,
    }

# ── 报告生成 ─────────────────────────────────────────────────────────────────

def generate_report(month_str: str, config_path: Path,
                    full_detail: bool = True,
                    cross_dedup: bool = True) -> Tuple[Dict, str]:
    year, month = int(month_str[:4]), int(month_str[5:7])
    cfg = json.loads(config_path.read_text(encoding='utf-8'))
    platform_name = cfg['platform_name']
    issue_type    = cfg.get('jira_issue_type', '支持问题')
    projs         = cfg['included_projects']

    print(f"[PlatformAnalysis] {platform_name}  {year}-{month:02d}  ({len(projs)} 个项目)")

    results = []
    for i, proj in enumerate(projs):
        print(f"  [{i+1:02d}/{len(projs)}] {proj['key']:<6} {proj['name'][:10]}...", end=' ', flush=True)
        r = analyze_project(proj, year, month, issue_type, full_detail)
        results.append(r)
        ok = "✅" if r['on_track_ipc'] else "⚠️"
        ipc_str = f"{r['month_ipc']}" if r['month_ipc'] else "N/A"
        print(f"{r['month_cnt']} 条 / {r['month_cust']} 客户 = {ipc_str} {ok}")

    # 平台汇总
    tot_cnt   = sum(r['month_cnt']  for r in results)
    tot_cust  = len({})   # 跨项目客户去重需要单独查，此处用加总近似
    tot_prev  = sum(r['prev_cnt']   for r in results)
    tot_yoy   = sum(r['yoy_cnt']    for r in results)
    tot_ytd   = sum(r['ytd_cnt']    for r in results)
    tot_yytd  = sum(r['yytd_cnt']   for r in results)
    tot_bl    = sum(r['baseline_2025'] for r in results)
    tot_bl_c  = sum(r['baseline_2025_customers'] for r in results)
    tot_bl_ipc = round(tot_bl / tot_bl_c, 2) if tot_bl_c else None
    # 平台加权 IPC（各项目客户数加总近似，仅供参考；达标基于各项目 YTD IPC）
    tot_month_cust = sum(r['month_cust'] for r in results)
    tot_ytd_cust   = sum(r['ytd_cust']   for r in results)
    tot_yytd_cust  = sum(r['yytd_cust']  for r in results)
    tot_month_ipc = round(tot_cnt / tot_month_cust, 2) if tot_month_cust else None
    tot_ytd_ipc   = round(tot_ytd / tot_ytd_cust, 2)   if tot_ytd_cust  else None
    tot_yytd_ipc  = round(tot_yytd / tot_yytd_cust, 2) if tot_yytd_cust else None
    target_ipc = round(tot_bl_ipc * 0.8, 2) if tot_bl_ipc else None

    # 跨项目真实去重 IPC（同一客户跨项目只计一次）
    true_ytd_cust: Optional[int] = None
    true_ytd_ipc:  Optional[float] = None
    overlap_ratio_pct: Optional[float] = None
    high_impact_customers: List[Dict] = []
    if cross_dedup:
        proj_keys = [p['key'] for p in projs]
        ytds, ytde = ytd_range(year, month)
        print(f"  [跨项目去重] 合并查询 {len(proj_keys)} 个项目 YTD 真实客户数...", flush=True)
        _, true_ytd_cust, high_impact_customers = count_cross_dedup_customers(
            proj_keys, ytds, ytde, issue_type)
        true_ytd_ipc  = round(tot_ytd / true_ytd_cust, 3) if true_ytd_cust else None
        overlap_ratio_pct = round((tot_ytd_cust - true_ytd_cust) / tot_ytd_cust * 100, 1) \
                            if tot_ytd_cust else None
        print(f"  [跨项目去重] 真实客户: {true_ytd_cust:,}（重叠率 {overlap_ratio_pct}%），"
              f"真实IPC: {true_ytd_ipc}，跨项目重点客户: {len(high_impact_customers)} 家")

    platform = {
        "month_cnt": tot_cnt, "prev_cnt": tot_prev, "yoy_cnt": tot_yoy,
        "ytd_cnt": tot_ytd, "yytd_cnt": tot_yytd,
        "month_cust": tot_month_cust, "ytd_cust": tot_ytd_cust, "yytd_cust": tot_yytd_cust,
        "month_ipc": tot_month_ipc, "ytd_ipc": tot_ytd_ipc, "yytd_ipc": tot_yytd_ipc,
        "target_ipc": target_ipc,
        "baseline_2025": tot_bl, "baseline_monthly_avg": round(tot_bl / 12, 1),
        "baseline_ipc": tot_bl_ipc,
        "mom_cnt_pct": pct_chg(tot_cnt, tot_prev),
        "yoy_cnt_pct": pct_chg(tot_cnt, tot_yoy),
        "ytd_cnt_pct": pct_chg(tot_ytd, tot_yytd),
        "ytd_ipc_pct": pct_chg(tot_ytd_ipc, tot_yytd_ipc),
        # 跨项目真实去重
        "true_ytd_cust": true_ytd_cust,
        "true_ytd_ipc": true_ytd_ipc,
        "overlap_ratio_pct": overlap_ratio_pct,
        "high_impact_customer_count": len(high_impact_customers),
        # 达标基于各项目 YTD IPC（跨月去重）
        "on_track_count": sum(1 for r in results if r['on_track_ipc']),
        "off_track_count": sum(1 for r in results if not r['on_track_ipc']),
    }

    results_sorted = sorted(results, key=lambda x: -x['month_cnt'])
    json_data = {
        "report_month": f"{year}-{month:02d}",
        "generated_at": date.today().isoformat(),
        "platform_name": platform_name,
        "platform": platform,
        "projects": results_sorted,
        "high_impact_customers": high_impact_customers,
    }

    md = _render_markdown(json_data, year, month)
    return json_data, md

# ── Markdown 渲染 ─────────────────────────────────────────────────────────────

def _fmt_pct(v: Optional[float], prefix: str = "") -> str:
    if v is None:
        return "—"
    arrow = "▼" if v < 0 else "▲"
    color_hint = "🟢" if v < 0 else "🔴"
    return f"{color_hint}{arrow}{abs(v):.1f}%"

def _fmt_ipc(v: Optional[float]) -> str:
    return f"**{v}**" if v is not None else "—"

def _risk_badge(r: Dict) -> str:
    return r['risk_level']

def _infer_deterioration(r: Dict) -> Tuple[str, str]:
    """推断 YTD IPC 恶化根因，返回 (根因描述, 策略方向)"""
    cnt_pct  = r.get('ytd_cnt_pct') or 0
    ipc_pct  = r.get('ytd_ipc_pct') or 0
    cust_now = r.get('ytd_cust', 0)
    cust_pre = r.get('yytd_cust', 0)
    cust_chg = round((cust_now - cust_pre) / cust_pre * 100, 1) if cust_pre else 0

    if cnt_pct > 5 and ipc_pct > 0:
        cause = f"工单量增加（YTD▲{cnt_pct:.1f}%），客户增长未能摊薄（{cust_pre:,}→{cust_now:,}，{cust_chg:+.1f}%）"
        strategy = "优先推进 **S1 自助赋能**（减少操作类重复工单）+ **S3 智能诊断**（降低报错/环境类问题）"
    elif cnt_pct < -5 and ipc_pct > 0:
        cause = f"工单量虽减少（YTD{cnt_pct:+.1f}%），但客户数缩减更快（{cust_pre:,}→{cust_now:,}，{cust_chg:+.1f}%），存量客户问题密度上升"
        strategy = "优先推进 **S2 方案模板**（解决重复性问题）+ **S4 开放生态**（降低高频客户依赖）"
    else:
        cause = f"客户结构变化（{cust_pre:,}→{cust_now:,}，{cust_chg:+.1f}%），高频客户占比上升导致 IPC 增加"
        strategy = "优先推进 **S2 方案模板** + 高频客户专项赋能"
    return cause, strategy

def _load_historical_data(output_dir: Path, year: int, current_month: int) -> Dict[str, Dict[int, Dict]]:
    """读取已有月报 JSON，返回 {proj_key: {month: {ytd_cnt, ytd_cust, ytd_ipc}}}"""
    result: Dict[str, Dict[int, Dict]] = {}
    for m in range(1, current_month + 1):
        tag = f"{year:04d}{m:02d}"
        fp  = output_dir / f"Platform_Report_{tag}.json"
        if not fp.exists():
            continue
        try:
            data = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:
            continue
        for proj in data.get('projects', []):
            key = proj['key']
            if key not in result:
                result[key] = {}
            result[key][m] = {
                'ytd_cnt':  proj.get('ytd_cnt', 0),
                'ytd_cust': proj.get('ytd_cust', 0),
                'ytd_ipc':  proj.get('ytd_ipc'),
                'ytd_ipc_pct': proj.get('ytd_ipc_pct'),
                'on_track_ipc': proj.get('on_track_ipc', True),
            }
    return result

def _render_strategy_report(json_data: Dict, year: int, month: int, output_dir: Path) -> str:
    """生成 Strategy_Report_YYYYMM.md（TOP10 S1-S4策略计划 + 月度检查核验表）"""
    ps     = {r['key']: r for r in json_data['projects']}
    pname  = json_data['platform_name']
    hist   = _load_historical_data(output_dir, year, month)

    # TOP10：按 baseline_2025 降序取前10（config 已按量排序）
    top10 = sorted(json_data['projects'], key=lambda x: -x['baseline_2025'])[:10]

    lines = [
        f"# {pname} 降工单策略计划 {year}-{month:02d}",
        f"",
        f"> 生成时间：{json_data['generated_at']}  ·  覆盖项目：TOP10（按2025年工单量）",
        f"",
        f"---",
        f"",
        f"## 一、策略框架（S1-S4）",
        f"",
        f"| 策略 | 名称 | 目标问题类型 | 预计覆盖消除量 |",
        f"|------|------|------------|-------------|",
        f"| **S1** | 自助赋能体系 | 操作咨询/配置问题（用户操作类） | 年度消除目标的 35% |",
        f"| **S2** | 方案模板知识库 | 实施配置/部署问题（重复性解决） | 年度消除目标的 25% |",
        f"| **S3** | 智能诊断工具 | 异常报错/环境/数据问题（需诊断类） | 年度消除目标的 25% |",
        f"| **S4** | 开放生态建设 | 二次开发/集成问题（开发者类） | 年度消除目标的 15% |",
        f"",
        f"> 各策略消除目标 = 项目年度消除目标（baseline×20%）× 策略占比",
        f"> 月度 IPC 目标（线性渐进）= baseline_ipc × (1 − 0.20 × 当前月份/12)",
        f"",
        f"---",
        f"",
        f"## 二、各项目策略计划",
        f"",
    ]

    for i, r in enumerate(top10, 1):
        key   = r['key']
        name  = r['name']
        bl    = r['baseline_2025']       # 2025全年工单
        bl_c  = r['baseline_2025_customers']
        bl_ipc = r['baseline_ipc']
        target_ipc = r['target_ipc']
        elim_annual = round(bl * 0.2)    # 年度消除目标（条）
        elim_monthly = round(elim_annual / 12)
        monthly_target_cnt = round(bl / 12 * 0.8)

        s1 = round(elim_annual * 0.35)
        s2 = round(elim_annual * 0.25)
        s3 = round(elim_annual * 0.25)
        s4 = elim_annual - s1 - s2 - s3

        # 当前 YTD 数据
        cur = ps.get(key, {})
        cur_ytd_ipc = cur.get('ytd_ipc', '—')
        cur_ytd_cnt = cur.get('ytd_cnt', 0)

        lines += [
            f"### {i}. {name}（{key}）",
            f"",
            f"| 基准 | 数值 |",
            f"|------|------|",
            f"| 2025年工单量 | {bl:,} 条 |",
            f"| 2025年客户数 | {bl_c:,} 家 |",
            f"| 2025 IPC 基准 | {bl_ipc} |",
            f"| 2026 目标 IPC | **{target_ipc}**（↓20%） |",
            f"| 年度消除目标 | {elim_annual:,} 条（≈{elim_monthly}/月） |",
            f"| 月均工单目标 | ≤{monthly_target_cnt:,} 条/月 |",
            f"",
            f"#### S1 自助赋能体系",
            f"",
            f"- **目标**：消除 {s1:,} 条/年（操作咨询/配置类工单）",
            f"- Q2 里程碑：整理 TOP20 操作类工单，上线知识库文章 / FAQ 页面",
            f"- Q3 里程碑：自助命中率 >30%，自助拦截工单 >{s1//2:,} 条",
            f"- Q4 里程碑：完善引导流程，全年自助消除 >{s1:,} 条",
            f"- 负责人：[ 待指定 ]",
            f"",
            f"#### S2 方案模板知识库",
            f"",
            f"- **目标**：消除 {s2:,} 条/年（重复实施/部署问题）",
            f"- Q2 里程碑：梳理 TOP10 高频问题，建立标准解决方案模板",
            f"- Q3 里程碑：推广使用，客服引用率 >50%",
            f"- Q4 里程碑：全年模板覆盖消除 >{s2:,} 条",
            f"- 负责人：[ 待指定 ]",
            f"",
            f"#### S3 智能诊断工具",
            f"",
            f"- **目标**：消除 {s3:,} 条/年（异常报错/环境/数据问题）",
            f"- Q2 里程碑：识别 TOP5 可诊断异常类型，设计诊断引导流程",
            f"- Q3 里程碑：上线诊断工具/文档，自助诊断率 >20%",
            f"- Q4 里程碑：全年工具辅助消除 >{s3:,} 条",
            f"- 负责人：[ 待指定 ]",
            f"",
            f"#### S4 开放生态建设",
            f"",
            f"- **目标**：消除 {s4:,} 条/年（二次开发/集成问题）",
            f"- Q2 里程碑：梳理开发类工单来源，补充 API 文档/示例代码",
            f"- Q3 里程碑：上线开发者社区或 SDK 更新，减少集成问题",
            f"- Q4 里程碑：全年开发者赋能消除 >{s4:,} 条",
            f"- 负责人：[ 待指定 ]",
            f"",
            f"#### 月度 YTD IPC 目标进度（{year}）",
            f"",
            f"| 月份 | YTD工单累积目标 | YTD IPC目标 | YTD IPC实际 | YTD同比 | 达标 |",
            f"|------|--------------|-----------|-----------|--------|------|",
        ]

        for m in range(1, 13):
            ipc_tgt = round(bl_ipc * (1 - 0.20 * m / 12), 2)
            cnt_tgt = round(bl / 12 * 0.8 * m)
            ph = hist.get(key, {}).get(m)
            if ph:
                actual_ipc  = ph.get('ytd_ipc', '—')
                actual_pct  = _fmt_pct(ph.get('ytd_ipc_pct'))
                ok = "✅" if ph.get('on_track_ipc', True) else "⚠️"
            elif m == month:
                actual_ipc  = cur_ytd_ipc
                actual_pct  = _fmt_pct(cur.get('ytd_ipc_pct'))
                ok = "✅" if cur.get('on_track_ipc', True) else "⚠️"
            else:
                actual_ipc, actual_pct, ok = "—", "—", "—"
            lines.append(
                f"| {year}-{m:02d} | ≤{cnt_tgt:,} | ≤{ipc_tgt} "
                f"| {actual_ipc} | {actual_pct} | {ok} |"
            )

        lines += ["", "---", ""]

    # 平台整体检查核验表
    lines += [
        f"## 三、平台整体月度检查核验表",
        f"",
        f"| 月份 | YTD工单目标 | YTD工单实际 | YTD IPC目标 | YTD IPC实际 | 达标项目 | 恶化项目 |",
        f"|------|-----------|-----------|-----------|-----------|---------|---------|",
    ]

    # 平台基准汇总
    plat_bl     = sum(r['baseline_2025'] for r in top10)
    plat_bl_c   = sum(r['baseline_2025_customers'] for r in top10)
    plat_bl_ipc = round(plat_bl / plat_bl_c, 2) if plat_bl_c else 0

    for m in range(1, 13):
        ipc_tgt  = round(plat_bl_ipc * (1 - 0.20 * m / 12), 2)
        cnt_tgt  = round(plat_bl / 12 * 0.8 * m)
        # 读取当月数据
        if m <= month:
            # 从当月或历史JSON里汇总 TOP10 数据
            tag = f"{year:04d}{m:02d}"
            fp  = output_dir / f"Platform_Report_{tag}.json"
            if fp.exists() or m == month:
                if m == month:
                    mdata = json_data
                else:
                    try:
                        mdata = json.loads(fp.read_text(encoding='utf-8'))
                    except Exception:
                        mdata = None
                if mdata:
                    m_projs = {p['key']: p for p in mdata['projects']}
                    m_cnt   = sum(m_projs[k]['ytd_cnt']  for k in [r['key'] for r in top10] if k in m_projs)
                    m_cust  = sum(m_projs[k]['ytd_cust'] for k in [r['key'] for r in top10] if k in m_projs)
                    m_ipc   = round(m_cnt / m_cust, 2) if m_cust else None
                    on_cnt  = sum(1 for k in [r['key'] for r in top10] if m_projs.get(k, {}).get('on_track_ipc', True))
                    det_cnt = sum(1 for k in [r['key'] for r in top10]
                                  if (m_projs.get(k, {}).get('ytd_ipc_pct') or 0) > 0)
                    ok      = "✅" if (m_ipc and m_ipc <= ipc_tgt) else "⚠️"
                    lines.append(
                        f"| {year}-{m:02d} | ≤{cnt_tgt:,} | {m_cnt:,} "
                        f"| ≤{ipc_tgt} | {m_ipc or '—'} | {on_cnt}/10 {ok} | {det_cnt}/10 |"
                    )
                    continue
            lines.append(f"| {year}-{m:02d} | ≤{cnt_tgt:,} | — | ≤{ipc_tgt} | — | — | — |")
        else:
            lines.append(f"| {year}-{m:02d} | ≤{cnt_tgt:,} | — | ≤{ipc_tgt} | — | — | — |")

    lines += [
        f"",
        f"> ★ 达标项目：YTD IPC ≤ 目标 IPC；恶化项目：YTD IPC 同比高于去年同期",
        f"",
    ]

    return "\n".join(lines)

def _render_markdown(data: Dict, year: int, month: int) -> str:
    p  = data['platform']
    ps = data['projects']
    pname = data['platform_name']

    # 风险项目
    high_risk = [r for r in ps if '🔴' in r['risk_level']]
    mid_risk  = [r for r in ps if '🟡' in r['risk_level']]
    off_track = [r for r in ps if not r['on_track_ipc']]
    off_track_sorted = sorted(off_track, key=lambda x: -(x['month_ipc'] or 0))

    lines = [
        f"# {pname} 降工单月报 {year}-{month:02d}",
        f"",
        f"> 生成时间：{data['generated_at']}  ·  覆盖项目：{len(ps)} 个  ·  数据来源：Jira",
        f"",
        f"---",
        f"",
        # ══ 一、核心 KPI 速览 ══════════════════════════════════════════
        f"## 一、核心 KPI 速览",
        f"",
        f"### 平台整体",
        f"",
        f"| 指标 | 2025基准 | 2026目标 | YTD实际 | YTD同比 | 本月参考IPC | 本月工单 |",
        f"|------|---------|---------|---------|--------|-----------|---------|",
        f"| **每客户问题数（IPC·逐项目去重）** | {p['baseline_ipc']} | {p['target_ipc']} "
        f"| {_fmt_ipc(p['ytd_ipc'])} | {_fmt_pct(p.get('ytd_ipc_pct'))} "
        f"| {p['month_ipc'] or '—'} | {p['month_cnt']:,} |",
    ] + ([
        f"| **真实IPC（跨项目去重★）** | — | — "
        f"| {_fmt_ipc(p.get('true_ytd_ipc'))} | — "
        f"| — | {p['true_ytd_cust']:,} 真实客户（重叠率{p['overlap_ratio_pct']}%） |",
    ] if p.get('true_ytd_ipc') else []) + [
        f"| 工单总量 | {p['baseline_monthly_avg']:,.0f}/月 | — "
        f"| {p['ytd_cnt']:,}（去年同期{p['yytd_cnt']:,}） | {_fmt_pct(p['ytd_cnt_pct'])} "
        f"| — | {_fmt_pct(p['yoy_cnt_pct'])}同比 |",
        f"",
        f"> **达标基准**：各项目 YTD IPC（跨月去重）≤ 目标 IPC。真实IPC★为跨项目去重，反映实际客户服务密度。",
        f"",
        f"**达标情况**：{p['on_track_count']} / {len(ps)} 个项目 YTD IPC 达标（≤ 目标值）",
        f"",
        f"### 各项目 KPI 一览",
        f"",
        f"| 级别 | Key | 项目 | 2025基准IPC | 目标IPC | YTD IPC ★ | YTD同比 | 本月IPC | 风险 |",
        f"|------|-----|------|-----------|--------|----------|--------|--------|------|",
    ]

    for r in sorted(ps, key=lambda x: (x['priority'], -x['baseline_ipc'])):
        lines.append(
            f"| {r['priority']} | {r['key']} | {r['name']} "
            f"| {r['baseline_ipc']} | {r['target_ipc']} | **{r['ytd_ipc'] or '—'}** "
            f"| {_fmt_pct(r['ytd_ipc_pct'])} | {r['month_ipc'] or '—'} "
            f"| {_risk_badge(r)} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        # ══ 二、平台月度概况 ═══════════════════════════════════════════
        f"## 二、平台月度概况",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 本月工单总量 | **{p['month_cnt']:,}** 条 |",
        f"| 环比上月 | {_fmt_pct(p['mom_cnt_pct'])} |",
        f"| 同比去年同月 | {_fmt_pct(p['yoy_cnt_pct'])} |",
        f"| 年初至今（YTD） | {p['ytd_cnt']:,} 条（去年同期 {p['yytd_cnt']:,}，{_fmt_pct(p['ytd_cnt_pct'])}） |",
        f"| 2025 基准月均 | {p['baseline_monthly_avg']:,} 条/月 |",
        f"",
        f"### 月度趋势（{year} vs {year-1}）",
        f"",
        f"| 月份 | {year-1} | {year} | 同比 |",
        f"|------|---------|---------|------|",
    ]

    # 月度趋势：汇总各项目的去年同月 + 本月（只能给出当月，历史月需单独查）
    lines.append(f"| {month:02d}月 | {p['yoy_cnt']:,} | {p['month_cnt']:,} | {_fmt_pct(p['yoy_cnt_pct'])} |")
    lines += [
        f"",
        f"> 注：历史各月数据见各月报 JSON。",
        f"",
        f"---",
        f"",
        # ══ 三、各项目详细指标 ════════════════════════════════════════
        f"## 三、各项目详细指标",
        f"",
        f"| 级 | Key | 项目 | 本月工单 | 本月IPC | 环比IPC | 同比IPC | YTD工单 | YTD客户 | YTD IPC★ | YTD同比 | 达标 |",
        f"|----|-----|------|---------|--------|--------|--------|--------|--------|---------|--------|------|",
    ]

    for r in ps:
        ok = "✅" if r['on_track_ipc'] else "⚠️"
        lines.append(
            f"| {r['priority']} | {r['key']} | {r['name']} "
            f"| {r['month_cnt']:,} | {r['month_ipc'] or '—'} "
            f"| {_fmt_pct(r['mom_ipc_pct'])} | {_fmt_pct(r['yoy_ipc_pct'])} "
            f"| {r['ytd_cnt']:,} | {r['ytd_cust']:,} | **{r['ytd_ipc'] or '—'}** "
            f"| {_fmt_pct(r['ytd_ipc_pct'])} | {ok} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        # ══ 四、风险分析 ═══════════════════════════════════════════════
        f"## 四、风险分析",
        f"",
    ]

    # ── YTD IPC 同比恶化预警（无论是否超目标，只要同比变差就列出）
    deteriorating = sorted(
        [r for r in ps if (r.get('ytd_ipc_pct') or 0) > 0],
        key=lambda x: -(x.get('ytd_ipc_pct') or 0)
    )
    if deteriorating:
        lines += [f"### ⚠️ YTD IPC 同比恶化预警（{len(deteriorating)} 个项目）", f""]
        for r in deteriorating:
            cause, strategy = _infer_deterioration(r)
            ipc_arrow = f"{r.get('yytd_ipc', '—')} → {r['ytd_ipc']}（{_fmt_pct(r.get('ytd_ipc_pct'))}）"
            cnt_arrow = f"{r.get('yytd_cnt', 0):,} → {r['ytd_cnt']:,}（{_fmt_pct(r.get('ytd_cnt_pct'))}）"
            lines += [
                f"**{r['key']} · {r['name']}**",
                f"- YTD IPC：{ipc_arrow}",
                f"- YTD工单：{cnt_arrow}",
                f"- 根因推断：{cause}",
                f"- 策略方向：{strategy}",
                f"",
            ]

    if high_risk:
        lines += [f"### 🔴 高风险项目（YTD IPC 超目标 且 YTD同比恶化 >10%）", f""]
        for r in high_risk:
            gap = round((r['ytd_ipc'] or 0) - r['target_ipc'], 2)
            lines += [
                f"**{r['key']} · {r['name']}**",
                f"- YTD IPC：{r['ytd_ipc']}（目标 {r['target_ipc']}，超出 **{gap}**）",
                f"- YTD：{r['ytd_cnt']:,} 条 / {r['ytd_cust']:,} 去重客户，同比 {_fmt_pct(r['ytd_ipc_pct'])}",
                f"- 本月：{r['month_cnt']:,} 条 / {r['month_cust']:,} 客户，单月同比 {_fmt_pct(r['yoy_ipc_pct'])}",
                f"- 建议：立即启动专项降工单分析，识别主要问题类型和高频客户",
                f"",
            ]

    if mid_risk:
        lines += [f"### 🟡 中风险项目（YTD IPC 超目标，YTD同比尚可）", f""]
        for r in mid_risk:
            gap = round((r['ytd_ipc'] or 0) - r['target_ipc'], 2)
            lines += [
                f"- **{r['key']} · {r['name']}**：YTD IPC {r['ytd_ipc']}（超目标 {gap}），YTD同比 {_fmt_pct(r['ytd_ipc_pct'])}",
            ]
        lines.append("")

    if not high_risk and not mid_risk:
        lines += [f"本月无高/中风险项目。 🎉", f""]

    # 整体趋势风险
    lines += [
        f"### 平台整体风险评估",
        f"",
        f"| 维度 | 状态 | 说明 |",
        f"|------|------|------|",
        f"| 工单量同比 | {'🔴 恶化' if (p['yoy_cnt_pct'] or 0) > 0 else '🟢 改善'} | {_fmt_pct(p['yoy_cnt_pct'])} |",
        f"| YTD工单量 | {'🔴 超去年同期' if (p['ytd_cnt_pct'] or 0) > 0 else '🟢 低于去年同期'} | {_fmt_pct(p['ytd_cnt_pct'])} |",
        f"| 超标项目数 | {'🔴' if p['off_track_count'] >= 8 else '🟡' if p['off_track_count'] >= 4 else '🟢'} {p['off_track_count']}/{len(ps)} | {'多数项目超标，需平台级干预' if p['off_track_count'] >= 8 else '部分项目超标' if p['off_track_count'] >= 4 else '整体可控'} |",
        f"",
        f"---",
        f"",
    ]

    # ══ 五、跨项目重点客户分析（仅在有数据时渲染）════════════════
    hi_custs = data.get('high_impact_customers', [])
    if hi_custs:
        lines += [
            f"## 五、跨项目重点客户分析",
            f"",
            f"> 以下客户在 **≥2 个平台项目** 同时有工单，其工单密度直接影响平台真实 IPC。",
            f"> 按涉及项目数（跨项目广度）+ 总工单量排序，建议优先开展客户专项赋能。",
            f"",
            f"| 排 | 客户 | 总工单 | 涉及项目 | 主要项目分布（工单量） |",
            f"|----|------|------|---------|---------------------|",
        ]
        proj_name_map = {r['key']: r['name'].replace('云平台-', '') for r in ps}
        for i, c in enumerate(hi_custs, 1):
            total_t = c['total_tickets']
            proj_cnt = c['project_count']
            dist = "、".join(
                f"{proj_name_map.get(k, k)}({v})"
                for k, v in list(c['by_project'].items())[:5]
            )
            lines.append(f"| {i} | {c['name']} | {total_t} | {proj_cnt} 个 | {dist} |")

        lines += [
            f"",
            f"### 重点客户跨项目分析建议",
            f"",
        ]
        # 涉及4+项目的超高影响力客户单独点评
        ultra = [c for c in hi_custs if c['project_count'] >= 4]
        if ultra:
            lines += [f"**超高影响力客户（涉及 ≥4 个项目）**", f""]
            for c in ultra[:5]:
                dist_detail = "、".join(
                    f"{proj_name_map.get(k, k)} {v}条"
                    for k, v in c['by_project'].items()
                )
                lines += [
                    f"- **{c['name']}**：{c['total_tickets']} 条工单跨 {c['project_count']} 个项目",
                    f"  分布：{dist_detail}",
                    f"  建议：对接客户成功团队，联合 P0 项目启动专项赋能，建立单一接入点减少重复提单",
                    f"",
                ]
        # 涉及2-3项目的客户概况
        mid_range = [c for c in hi_custs if 2 <= c['project_count'] <= 3]
        lines += [
            f"**跨项目（2-3个）重点客户**：共 {len(mid_range)} 家，",
            f"建议各项目 PdM 在月度复盘中核查名单，协同制定客户赋能计划。",
            f"",
        ]
        lines += [
            f"---",
            f"",
        ]

    lines += [
        # ══ 六、累积指标说明 ════════════════════════════════════════
        f"## 六、指标定义",
        f"",
        f"| 指标 | 定义 |",
        f"|------|------|",
        f"| **YTD IPC ★** | 年初至本月末工单总数 ÷ 同期去重客户数（跨月去重，为达标核心指标） |",
        f"| 本月 IPC | 当月工单 ÷ 当月去重客户（同一客户在不同月份各计一次，仅供趋势参考） |",
        f"| 2025 基准 IPC | 2025全年工单 ÷ 2025全年去重客户数（静态基准） |",
        f"| 目标 IPC | 基准 IPC × 80%（降低 20%） |",
        f"| YTD同比 | YTD IPC 与去年同期 YTD IPC 对比 |",
        f"| 达标 | YTD IPC ≤ 目标 IPC（使用累积去重客户，避免重复客户拉低分母） |",
        f"",
    ]

    return "\n".join(lines)

# ── 入口 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="应用与开发平台降工单月报")
    parser.add_argument("--month", required=True, help="YYYY-MM")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", help="输出目录")
    parser.add_argument("--no-detail", action="store_true", help="跳过多维度分析（快速模式）")
    parser.add_argument("--no-cross-dedup", action="store_true",
                        help="跳过跨项目真实去重IPC查询（省约60秒，适合快速预览）")
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = json.loads(config_path.read_text(encoding='utf-8'))
    output_dir = Path(args.output) if args.output else PROJECT_ROOT / cfg.get('output_dir', 'conclusion/_local/platform-reports')
    output_dir.mkdir(parents=True, exist_ok=True)

    year, month = int(args.month[:4]), int(args.month[5:7])
    json_data, md = generate_report(args.month, config_path,
                                    not args.no_detail,
                                    not args.no_cross_dedup)

    tag = args.month.replace("-", "")
    jp = output_dir / f"Platform_Report_{tag}.json"
    mp = output_dir / f"Platform_Report_{tag}.md"
    sp = output_dir / f"Strategy_Report_{tag}.md"

    jp.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding='utf-8')
    mp.write_text(md, encoding='utf-8')

    strategy_md = _render_strategy_report(json_data, year, month, output_dir)
    sp.write_text(strategy_md, encoding='utf-8')

    print(f"\n✅  JSON:     {jp}")
    print(f"✅  MD:       {mp}")
    print(f"✅  Strategy: {sp}")

if __name__ == "__main__":
    main()
