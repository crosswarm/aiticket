"""
Demo data manager for the dist branch HR demo.
Used by backend API endpoints (/api/admin/demo-status, install, clear).

Path resolution:
  - Dev:    DATA_DIR defaults to <project_root>/data  (detected by filesystem)
  - Docker: Set DATA_DIR=/data  KB_DIR=/data/kb in environment
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # APP/backend/ or /app in container

# Detect project root: in dev, parent.parent has data/ and KB/ directories
_PARENT2 = _HERE.parent.parent
_INFERRED_ROOT = _PARENT2 if (_PARENT2 / "data").is_dir() else None

_DATA_ROOT = Path(os.environ.get("DATA_DIR") or (str(_INFERRED_ROOT / "data") if _INFERRED_ROOT else "/data"))
_KB_ROOT = Path(os.environ.get("KB_DIR") or (str(_INFERRED_ROOT / "KB") if _INFERRED_ROOT else "/data/kb"))
_CONFIG_DIR = _HERE / "config"

DEMO_SEEDED_MARKER = _DATA_ROOT / ".demo_seeded"
DEMO_DISMISSED_MARKER = _DATA_ROOT / ".demo_prompt_dismissed"
DEMO_XLSX = _DATA_ROOT / "imports" / "demo_hr_tickets.xlsx"
DEMO_KB_HR_DIR = _KB_ROOT / "hr"
DEMO_CONFIG = _CONFIG_DIR / "deployment.yaml"
EXAMPLE_CONFIG = _CONFIG_DIR / "deployment.example.yaml"

# ── deployment.demo-hr.yaml content ──────────────────────────────────────────
_DEMO_DEPLOYMENT_YAML = """\
# AITicket Demo — HR 工单演示配置（由 install-demo-data 自动生成）
instance:
  name: "HR Demo Ticket"
  slug: "hr-demo"
  primary_project_key: "HR"
  allowed_project_keys:
    - "HR"

module_taxonomy:
  - name: "考勤"
    team: ""
    keywords: ["考勤", "迟到", "早退", "签到", "打卡", "旷工", "考勤异常", "考勤申诉"]
  - name: "请假"
    team: ""
    keywords: ["请假", "年假", "病假", "调休", "婚假", "产假", "事假", "假期"]
  - name: "入职"
    team: ""
    keywords: ["入职", "报到", "工牌", "社保", "公积金", "劳动合同", "试用期", "新员工"]
  - name: "薪酬"
    team: ""
    keywords: ["薪酬", "工资", "绩效", "加班费", "年终奖", "个税", "五险一金", "基本工资"]

kb:
  root_dir: "KB"

data_source:
  type: excel
  excel:
    file_path: "data/imports/demo_hr_tickets.xlsx"
    column_map:
      key:           "工单编号"
      reporter:      "提出者"
      contact_info:  "提出者联系方式"
      customer_name: "所属企业"
      summary:       "问题标题"
      description:   "问题描述"
      created:       "提出时间"
      status:        "当前状态"
      assignee:      "处理人"
      priority:      "优先级"
      issue_type:    "工单类型"
      project_name:  "业务模块"
"""

# ── Ticket rows ───────────────────────────────────────────────────────────────
_TICKET_COLS = ["工单编号", "提出者", "提出者联系方式", "所属企业",
                "问题标题", "问题描述", "提出时间", "当前状态",
                "处理人", "优先级", "工单类型", "业务模块"]

_TICKET_ROWS = [
    ["HR-001", "张伟", "13900139001", "上海某科技有限公司",
     "本月迟到几次才开始扣款",
     "我这个月迟到了2次，HR说还没到扣款线，想确认一下公司的迟到扣款规定是什么，从第几次迟到开始扣，每次扣多少钱。",
     "2026-05-06 09:15", "待处理", "人事-小赵", "中", "咨询", "考勤"],
    ["HR-002", "李娜", "13800138002", "北京某贸易有限公司",
     "打卡异常如何申诉",
     "上周三我在公司打卡但系统没有记录，导致考勤显示缺勤。请问考勤异常应该怎么申诉，有没有截止时间？",
     "2026-05-07 10:30", "处理中", "人事-小赵", "高", "申请", "考勤"],
    ["HR-003", "王芳", "18600001234", "广州某制造有限公司",
     "迟到30分钟和迟到10分钟有区别吗",
     "请问迟到30分钟和迟到10分钟在考勤处理上是否有区别？听说超过30分钟算旷工，想了解具体规定。",
     "2026-05-08 14:20", "已解决", "人事-小李", "低", "咨询", "考勤"],
    ["HR-004", "陈强", "17700002345", "深圳某互联网有限公司",
     "每月考勤数据什么时候统计",
     "想了解每月考勤数据的统计截止日期，以及什么时候可以看到当月的最终考勤结果。",
     "2026-05-09 08:45", "待处理", "人事-小李", "低", "咨询", "考勤"],
    ["HR-005", "刘洋", "13600003456", "成都某软件有限公司",
     "入职满一年有多少天年假",
     "我入职刚好满一年，想请年假，但不确定能享受多少天年假，请帮我确认一下。",
     "2026-05-10 11:00", "待处理", "人事-小赵", "中", "咨询", "请假"],
    ["HR-006", "赵敏", "13500004567", "杭州某电商有限公司",
     "请假流程是什么",
     "我需要请3天病假，不清楚具体的请假申请流程，是在钉钉上还是HR系统里申请？需要哪些材料？",
     "2026-05-11 09:30", "处理中", "人事-小李", "高", "申请", "请假"],
    ["HR-007", "周雷", "13400005678", "武汉某金融有限公司",
     "调休可以累积多少天",
     "我最近加班比较多，积累了不少调休，请问调休最多可以累积多少天，超过之后会怎么处理？",
     "2026-05-12 16:00", "已解决", "人事-小赵", "中", "咨询", "请假"],
    ["HR-008", "吴静", "18900006789", "南京某医疗有限公司",
     "病假需要提供什么证明",
     "我发烧需要请病假，请问病假需要提供哪些证明材料？超过几天需要额外手续？",
     "2026-05-13 08:00", "待处理", "人事-小李", "高", "申请", "请假"],
    ["HR-009", "孙浩", "13200007890", "西安某科技有限公司",
     "入职第一天需要带哪些材料",
     "我下周一正式入职，想了解报到当天需要携带什么材料，是否需要提前准备什么，以及当天会领到工牌吗？",
     "2026-05-14 10:15", "待处理", "人事-小赵", "中", "咨询", "入职"],
    ["HR-010", "马云云", "18700008901", "重庆某制造有限公司",
     "入职后多久开始缴社保",
     "我刚入职，不清楚社保是从入职当月开始缴还是下个月开始，公积金是同步的吗？",
     "2026-05-15 14:00", "处理中", "人事-小李", "中", "咨询", "入职"],
    ["HR-011", "何梅", "13100009012", "苏州某物流有限公司",
     "劳动合同什么时候签",
     "入职已经3天了，还没有签劳动合同，这正常吗？应该找谁签，什么时候截止？",
     "2026-05-16 09:45", "已解决", "人事-小赵", "高", "咨询", "入职"],
    ["HR-012", "郑伟", "18600000000", "天津某贸易有限公司",
     "试用期多长时间，转正考核怎么进行",
     "入职时HR说试用期3个月，想了解试用期内会有哪些考核，以及转正流程是什么。",
     "2026-05-17 11:30", "待处理", "人事-小李", "中", "咨询", "入职"],
    ["HR-013", "林志强", "13000001111", "厦门某互联网有限公司",
     "绩效工资是怎么计算的",
     "我的薪酬构成中有绩效工资，不清楚绩效系数是怎么评定的，绩效工资占总薪酬多少比例？",
     "2026-05-18 10:00", "待处理", "人事-小赵", "中", "咨询", "薪酬"],
    ["HR-014", "黄敏", "13100002222", "福州某科技有限公司",
     "周末加班费怎么算",
     "最近公司安排周末加班，想了解加班费的计算标准，周末加班和法定节假日加班有什么区别？",
     "2026-05-19 09:00", "处理中", "人事-小李", "高", "咨询", "薪酬"],
    ["HR-015", "曹磊", "13200003333", "合肥某制造有限公司",
     "个税怎么扣，有什么专项扣除",
     "想了解工资中的个人所得税是怎么计算和扣缴的，有哪些专项附加扣除可以申报？",
     "2026-05-19 15:30", "待处理", "人事-小赵", "低", "咨询", "薪酬"],
    ["HR-016", "田娟", "13300004444", "无锡某电商有限公司",
     "年终奖什么时候发，怎么计算",
     "听说公司有年终奖，请问年终奖一般什么时候发放，计算标准是什么，跟绩效挂钩吗？",
     "2026-05-20 08:30", "待处理", "人事-小李", "中", "咨询", "薪酬"],
]

# ── KB content ────────────────────────────────────────────────────────────────
_ATTENDANCE_TXT = """\
# HR管理系统 - 考勤管理制度

## 一、工作时间

正常工作时间：每天 9:00 - 18:00，午休 12:00 - 13:30。
弹性签到时间：9:00 至 10:00 之间签到均视为正常到岗。
每周工作5天，周六日为法定休息日。

## 二、迟到处理规定

1. 每月迟到 3 次以内（含3次）不做扣款处理。
2. 超过 3 次后，每次迟到扣款 50 元。
3. 迟到 30 分钟以上计旷工半天；旷工处理标准：日薪 / 2。
4. 全月旷工超过 3 天者，视情节轻重给予纪律处分。

## 三、早退处理

早退处理标准与迟到相同，累计计入当月迟到/早退次数。

## 四、考勤异常申诉

1. 考勤异常须在异常发生后 **24小时内** 提交申诉，超期不予受理。
2. 申诉渠道：通过HR系统「考勤管理」-「异常申诉」模块提交，须附打卡截图或证明材料。
3. 申诉审批周期：1-3 个工作日，结果通过邮件和HR系统通知。
4. 申诉截止日：每月 **25日** 为当月考勤截止日，25日后不再受理当月异常申诉。

## 五、月度考勤统计

- 每月 25 日为考勤数据统计截止日。
- HR 部门在每月 28 日前完成考勤数据核对，并发送至财务部门。
- 员工可在HR系统查看当月考勤汇总，如有异议请在截止日前提出。

## 六、加班登记

1. 工作日 18:00 后加班须提前申请，经直属上级审批。
2. 周末加班须提前 1 天提交加班申请。
3. 加班可选择调休（优先）或加班费（见薪酬制度）。
"""

_LEAVE_MD = """\
# HR管理系统 - 请假申请指南

## 一、年假政策

根据《职工带薪年休假条例》及公司规定，年假天数按工龄计算：

| 工龄 | 年假天数 |
|------|--------|
| 不满1年 | 5天 |
| 满1年不满10年 | 10天 |
| 满10年以上 | 15天 |

注：工龄以社保缴纳年限为准，跨公司工龄可累积。

## 二、病假规定

1. 病假须提供正规医院（二级及以上）的诊断证明。
2. 连续病假超过 3 天需向 HR 备案并提交医院证明原件。
3. 连续病假超过 30 天，须提交复工证明方可回岗。
4. 病假期间工资按基本工资的 80% 发放。

## 三、调休管理

1. 加班调休可累积保留，最多累积 **90天**。
2. 累积调休超过 90 天的部分自动作废，不计为加班费。
3. 调休须提前 1 天提交申请，经直属上级审批。
4. 年底（12月31日）未使用的调休不予结转。

## 四、请假申请流程（重要步骤）

步骤1：登录钉钉 HR 系统，进入【假勤管理】-【请假申请】。
步骤2：选择请假类型（年假/病假/调休/事假等）和起止时间，填写请假事由。
步骤3：系统自动推送至直属上级审批（须在 1 个工作日内完成审批）。
步骤4：上级审批通过后，HR 备案（仅病假、婚假、产假需要额外备案）。
步骤5：请假结束后，如涉及调休请及时在系统中标注，确保考勤数据准确。

## 五、特殊假种说明

| 假种 | 天数 | 所需证明 |
|------|------|--------|
| 婚假 | 3天 | 结婚证 |
| 产假 | 158天（法定98天+生育奖励假60天） | 出生医学证明 |
| 陪产假 | 15天 | 出生医学证明 |
| 丧假 | 3天（直系亲属） | 死亡证明 |

## 六、注意事项

- 未经审批擅自离岗视为旷工处理。
- 超出年假剩余天数的，超出部分按事假处理（扣除相应日薪）。
- 请假记录可在钉钉 HR 系统「假勤查询」模块查询。
"""

_SALARY_MD = """\
# 薪酬结构说明

本文档说明公司薪酬各组成部分的计算方式与发放频率。

## 薪酬组成

| 薪酬项 | 计算口径 | 发放频率 | 备注 |
|--------|----------|----------|------|
| 基本工资 | 月薪 × 70% | 每月 | 固定部分，不与绩效挂钩 |
| 绩效工资 | 月薪 × 30% × 绩效系数（0.5-1.5） | 每月 | 绩效系数由直属上级与 HR 共同评定 |
| 加班费 | 时薪 × 加班时长 × 倍数 | 每月 | 平日 1.5 倍 / 周末 2.0 倍 / 法定节假日 3.0 倍 |
| 年终奖 | 平均月薪 × N 个月 | 每年 | N 由公司根据年度经营情况决定，一般 1-3 个月 |
| 餐补 | 固定 200 元/月 | 每月 | 在职当月全额发放，离职当月按出勤比例发放 |
| 交通补贴 | 固定 300 元/月 | 每月 | 出差期间暂停发放，报销出差交通费 |
| 个税 | 按累计预扣法代扣 | 每月代扣 | 遵照国家税务总局最新规定，支持专项附加扣除 |
| 养老保险 | 缴费基数 × 8%（个人） | 每月 | 公司缴纳 16%，个人缴纳 8% |
| 医疗保险 | 缴费基数 × 2%（个人） | 每月 | 公司缴纳 10%，个人缴纳 2% |
| 失业保险 | 缴费基数 × 0.5%（个人） | 每月 | 公司缴纳 0.5%，个人缴纳 0.5% |
| 住房公积金 | 缴存基数 × 12%（个人） | 每月 | 公司 12%，个人 12%，共计 24% |

## 绩效系数说明

- 优秀（S）：系数 1.5；良好（A）：1.2；合格（B）：1.0；待改进（C）：0.7；不合格（D）：0.5

## 加班费计算

时薪 = 月薪 ÷ 21.75 ÷ 8；平日 1.5 倍 / 周末 2.0 倍 / 法定节假日 3.0 倍

## 个税专项扣除

子女教育每月 1000 元 / 住房贷款利息每月 1000 元 / 赡养老人最高 2000 元

## 发放时间

每月 15 日前发放上月薪酬（遇节假日顺延）。
"""

_ONBOARDING_MD = """\
# HR管理系统 - 新员工入职手册

## 一、入职前准备

- 收到录用通知后，登录 HR 系统完善个人信息（含银行账户、紧急联系人、学历信息等）。
- 准备以下材料原件：身份证、毕业证、离职证明（如有）、近期一寸照片 2 张。
- 提前了解公司位置和报到时间，确认入职当天的具体安排。

## 二、报到当天（Day 1）

- 报到时间：上午 9:00 前至前台签到，迟到须提前通知 HR。
- 领取物品：工牌（含门禁权限，当天生效）、工作笔记本电脑（IT 当天完成初始化配置）、办公文具一套、公司手册及员工守则（纸质版）。
- 参加新员工入职培训（人力资源部组织，上午 10:00 开始）。

## 三、入职一周内必须完成

- 社保登记：HR 在入职 3 个工作日内完成社保参保手续，次月起生效。
- 公积金账户：在职当月满 15 天的，当月开始缴公积金；不满 15 天的次月起缴。
- 签订劳动合同：入职 1 周内完成劳动合同签署（电子版，通过 HR 系统完成）。
- 账号开通：IT 在入职 1 个工作日内完成工作邮箱、内网账号、飞书账号开通。
- 工牌激活：在 HR 系统绑定工号，完成照片更新。

## 四、试用期安排

- 试用期：90 天，从入职当日起计算（法定最长不超过 6 个月）。
- 期间考核：第 30 天、第 60 天各进行一次绩效面谈，结果记入转正评估。
- 转正评估：第 85 天前由直属上级提交转正申请，HR 审核后通知员工。
- 试用期工资：通常为正式工资的 80%（具体以 Offer 为准）。

## 五、导师（Mentor）计划

- 每位新员工配备 1 名 Mentor（入职前由 HR 协调配对，同部门资深员工担任）。
- Mentor 陪同时间：前 30 天每周至少 1 次面谈（≥30 分钟）。
- Mentor 职责：业务导向、系统培训、文化融入、答疑解惑。
"""

_KB_README = """\
# HR 管理系统知识库

本目录存放人力资源管理系统相关政策和操作指南，供 AI 智能回复检索使用。

## 文档清单

| 文件 | 内容 |
|------|------|
| 01_attendance_policy.txt | 考勤管理制度（迟到/早退/旷工/申诉规则）|
| 02_leave_application_guide.md | 请假申请流程（年假/病假/调休/各类假种）|
| 03_onboarding_checklist.md | 新员工入职手册（报到/社保/试用期/导师）|
| 04_salary_components.md | 薪酬构成说明（各项计算口径和发放频率）|

## 使用方式

```bash
curl -X POST http://localhost:18000/api/kb/sync
```
"""


# ── Public API ────────────────────────────────────────────────────────────────

def get_demo_status() -> dict:
    return {
        "seeded": DEMO_SEEDED_MARKER.exists(),
        "prompt_dismissed": DEMO_DISMISSED_MARKER.exists(),
        "files_present": {
            "xlsx": DEMO_XLSX.exists(),
            "kb_hr": DEMO_KB_HR_DIR.is_dir() and any(DEMO_KB_HR_DIR.iterdir()),
        },
    }


def seed_all(force: bool = False) -> dict:
    """Create all demo data files. Returns summary dict."""
    _ensure_dirs()
    _write_xlsx(force)
    _write_kb_files(force)
    _write_demo_deployment_yaml()
    return {
        "xlsx_path": str(DEMO_XLSX),
        "issues_count": len(_TICKET_ROWS),
        "kb_files": [str(p) for p in DEMO_KB_HR_DIR.glob("*") if p.suffix in (".txt", ".md", ".docx", ".xlsx")],
    }


def clear_all() -> dict:
    """Remove all demo files and revert deployment.yaml. Returns summary dict."""
    removed = []
    errors = []

    # 1. Remove xlsx
    _try_remove(DEMO_XLSX, removed, errors, "xlsx")

    # 2. Remove KB/hr/ directory
    if DEMO_KB_HR_DIR.exists():
        try:
            shutil.rmtree(str(DEMO_KB_HR_DIR))
            removed.append(str(DEMO_KB_HR_DIR))
        except Exception as e:
            errors.append(f"KB/hr: {e}")

    # 3. Revert deployment.yaml to example
    try:
        if EXAMPLE_CONFIG.exists():
            shutil.copy2(str(EXAMPLE_CONFIG), str(DEMO_CONFIG))
            removed.append(f"reverted {DEMO_CONFIG.name}")
        else:
            # Write a minimal blank config
            DEMO_CONFIG.write_text("# deployment.yaml — configure your Jira settings here\n", encoding="utf-8")
            removed.append(f"reset {DEMO_CONFIG.name}")
    except Exception as e:
        errors.append(f"deployment.yaml revert: {e}")

    # 4. Clear Excel board cache
    cache_paths = [
        _HERE / "data" / "cache" / "excel_board.json",
        _DATA_ROOT / "cache" / "excel_board.json",
    ]
    for cp in cache_paths:
        _try_remove(cp, removed, errors, f"cache:{cp.name}")

    # 5. Clear HR reply cache entries
    reply_cache_paths = [
        _HERE / "data_cache" / "reply_cache.json",
        _DATA_ROOT / "reply_cache.json",
    ]
    for rcp in reply_cache_paths:
        _clear_hr_reply_cache(rcp, removed, errors)

    # 6. Write dismissed marker (keep seeded marker so prompt doesn't re-appear)
    try:
        DEMO_DISMISSED_MARKER.parent.mkdir(parents=True, exist_ok=True)
        DEMO_DISMISSED_MARKER.touch()
    except Exception as e:
        errors.append(f"dismissed marker: {e}")

    return {"success": len(errors) == 0, "cleared_items": removed, "errors": errors}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ensure_dirs():
    for d in [DEMO_XLSX.parent, DEMO_KB_HR_DIR, _CONFIG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def _write_xlsx(force: bool):
    if DEMO_XLSX.exists() and not force:
        return
    try:
        import pandas as pd
        df = pd.DataFrame(_TICKET_ROWS, columns=_TICKET_COLS).astype(str)
        df.to_excel(str(DEMO_XLSX), index=False, engine="openpyxl")
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "pandas", "openpyxl"])
        import pandas as pd
        df = pd.DataFrame(_TICKET_ROWS, columns=_TICKET_COLS).astype(str)
        df.to_excel(str(DEMO_XLSX), index=False, engine="openpyxl")


def _write_kb_files(force: bool):
    files = {
        DEMO_KB_HR_DIR / "01_attendance_policy.txt": _ATTENDANCE_TXT,
        DEMO_KB_HR_DIR / "02_leave_application_guide.md": _LEAVE_MD,
        DEMO_KB_HR_DIR / "03_onboarding_checklist.md": _ONBOARDING_MD,
        DEMO_KB_HR_DIR / "04_salary_components.md": _SALARY_MD,
        DEMO_KB_HR_DIR / "README.md": _KB_README,
    }
    for path, content in files.items():
        if not path.exists() or force:
            path.write_text(content, encoding="utf-8")


def _write_demo_deployment_yaml():
    DEMO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    DEMO_CONFIG.write_text(_DEMO_DEPLOYMENT_YAML, encoding="utf-8")


def _try_remove(path: Path, removed: list, errors: list, label: str):
    if path.exists():
        try:
            path.unlink()
            removed.append(str(path))
        except Exception as e:
            errors.append(f"{label}: {e}")


def _clear_hr_reply_cache(cache_path: Path, removed: list, errors: list):
    if not cache_path.exists():
        return
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        original_size = len(data)
        cleaned = {k: v for k, v in data.items() if not k.startswith("excel:HR-")}
        if len(cleaned) < original_size:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            removed.append(f"cleared {original_size - len(cleaned)} HR reply cache entries")
    except Exception as e:
        errors.append(f"reply_cache cleanup: {e}")
