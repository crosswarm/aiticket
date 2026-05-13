"""
PRD Agent Team 多轮对抗辩论编排器

用 5 种角色 × 3 轮辩论，基于真实数据举证分析，产出
conclusion/_local/prd/prd_module_aware_reply.md

运行：
  python APP/backend/services/debate_orchestrator.py --topic "module-aware reply" --rounds 3
  python APP/backend/services/debate_orchestrator.py --topic "module-aware reply" --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent
DEBATE_DIR   = PROJECT_ROOT / "conclusion" / "_local" / "debate"
PRD_DIR      = PROJECT_ROOT / "conclusion" / "_local" / "prd"

sys.path.insert(0, str(BACKEND_DIR))

# ─── Per-role LLM assignment ──────────────────────────────────────────────────
# 每个角色的 provider 偏好链。
# 格式支持两种：
#   字符串     → 使用 llm_config.json 中该 provider 的默认 model
#   字典       → {"provider": "minimax", "model": "MiniMax-Text-01"} 覆盖 model
#
# 已验证可用（2026-05-06）：minimax ✅  local(SuperGemma4) ✅
# 不可用：aliyun/openai(401)  kimi(404)  zhipu(未配置)
#
# 角色设计逻辑：
#   proposer  → minimax 首选（快、结构化输出好，适合建设性论证）
#   critic    → local 首选（SuperGemma4 uncensored，批判性推理更直接）
#   ux        → minimax 首选（场景描述细腻）
#   judge     → minimax 首选（M2.7 推理链明确，适合打分）
#   verifier  → local 首选（SuperGemma4 26B 长文综合能力，最终 PRD 质量更高）
ROLE_LLM: dict[str, list] = {
    "proposer": ["minimax", "local"],
    "critic":   ["local",   "minimax"],
    "ux":       ["minimax", "local"],
    "judge":    ["minimax", "local"],
    "verifier": ["local",   "minimax"],
}


# ─── LLM client (reuse jobmaster pattern) ────────────────────────────────────

def _load_cfg() -> dict:
    return json.loads((BACKEND_DIR / "llm_config.json").read_text(encoding="utf-8"))


def _call_llm(provider: str, system: str, user: str,
              max_tokens: int = 2500, cfg: dict | None = None,
              model_override: str | None = None) -> str:
    import requests as _req
    if cfg is None:
        cfg = _load_cfg()
    p = cfg.get(provider, {})
    if not isinstance(p, dict):
        return f"[{provider} 配置格式错误]"
    base_url = p.get("base_url", "")
    api_key  = p.get("api_key", "")
    model    = model_override or p.get("model_name", "")
    if not base_url or not api_key:
        return f"[{provider} 未配置]"
    session = _req.Session()
    if "localhost" in base_url or "127.0.0.1" in base_url:
        session.trust_env = False
    try:
        r = session.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "system", "content": system},
                                {"role": "user",   "content": user}],
                  "max_tokens": max_tokens,
                  "temperature": 0.65},
            timeout=180,
        )
        r.raise_for_status()
        d = r.json()
        msg = d.get("choices", [{}])[0].get("message", {})
        return msg.get("content") or msg.get("reasoning_content") or "[空回复]"
    except Exception as exc:
        return f"[LLM异常: {exc}]"


def _call_chain(system: str, user: str, max_tokens: int = 2500,
                role: str | None = None) -> str:
    """按角色偏好顺序尝试 LLM provider，失败则依次 fallback。"""
    cfg = _load_cfg()
    # 根据角色选 provider 顺序，默认全局链
    providers = ROLE_LLM.get(role or "", ["minimax", "local", "aliyun", "openai", "kimi"])

    try:
        from services.local_llm_lifecycle import is_alive as _local_alive  # type: ignore
        local_ok = _local_alive()
    except Exception:
        local_ok = False

    for entry in providers:
        # entry 可以是 str 或 {"provider": "...", "model": "..."}
        if isinstance(entry, dict):
            provider       = entry["provider"]
            model_override = entry.get("model")
        else:
            provider       = entry
            model_override = None

        if provider == "local" and not local_ok:
            continue
        p_cfg = cfg.get(provider, {})
        if not isinstance(p_cfg, dict) or not p_cfg.get("api_key") or not p_cfg.get("base_url"):
            continue
        result = _call_llm(provider, system, user, max_tokens, cfg, model_override)
        if not result.startswith("["):
            label = f"{provider}({model_override})" if model_override else provider
            print(f"    [{role or '?'}] 使用 {label}")
            return result
        print(f"    [{role or '?'}] {provider} 失败: {result[:80]}", file=sys.stderr)

    return "[所有 LLM provider 均失败]"


# ─── Real context loader ─────────────────────────────────────────────────────

def _load_context() -> dict:
    ctx: dict = {}

    # 1. Adoption stats
    try:
        fb = json.loads((BACKEND_DIR / "data" / "reply_feedback.json").read_text())
        ctx["adoption_rate"] = fb.get("live_adoption_rate", "?")
        ctx["live_total"]    = fb.get("live_total", 0)
        ctx["live_adopted"]  = fb.get("live_adopted", 0)
        ctx["live_modified"] = fb.get("live_modified", 0)
    except Exception:
        ctx["adoption_rate"] = "2.7%"; ctx["live_total"] = 224

    # 2. KB stats
    try:
        import sqlite3
        conn = sqlite3.connect(str(PROJECT_ROOT / "data" / "sqlite" / "kb_chunks.db"))
        c = conn.cursor()
        c.execute("SELECT l1_module, COUNT(*) FROM documents GROUP BY l1_module ORDER BY 2 DESC LIMIT 10")
        ctx["kb_top_modules"] = c.fetchall()
        c.execute("SELECT COUNT(*) FROM documents"); ctx["kb_docs"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM chunks");    ctx["kb_chunks"] = c.fetchone()[0]
        conn.close()
    except Exception:
        ctx["kb_docs"] = 1493; ctx["kb_top_modules"] = []; ctx["kb_chunks"] = 7932

    # 3. KB gaps
    try:
        gaps_path = PROJECT_ROOT / "conclusion" / "kb_gaps.jsonl"
        gaps = [json.loads(l) for l in gaps_path.read_text().splitlines() if l.strip()]
        topic_count: dict[str, int] = {}
        for g in gaps:
            t = g.get("topic_inferred", "未知")
            topic_count[t] = topic_count.get(t, 0) + 1
        ctx["gap_topics"] = sorted(topic_count.items(), key=lambda x: -x[1])[:10]
        ctx["gap_total"]  = len(gaps)
        ctx["gap_kb_miss"] = sum(1 for g in gaps if "暂未查到" in g.get("ai_reply", "")
                                 or "未查到相关" in g.get("ai_reply", ""))
    except Exception:
        ctx["gap_total"] = 130; ctx["gap_topics"] = []

    # 4. Style rules summary
    try:
        rules_path = BACKEND_DIR / "data" / "reply_style_rules.md"
        ctx["style_rules_summary"] = rules_path.read_text(encoding="utf-8")[:1500]
    except Exception:
        ctx["style_rules_summary"] = "（读取失败）"

    # 5. Training metrics
    try:
        metrics_path = PROJECT_ROOT / "conclusion" / "_local" / "training" / "training_metrics.jsonl"
        lines = metrics_path.read_text().strip().splitlines()[-3:]
        ctx["training_metrics"] = [json.loads(l) for l in lines if l.strip()]
    except Exception:
        ctx["training_metrics"] = []

    return ctx


def _format_context(ctx: dict) -> str:
    km = "\n".join(f"    {m[0] or '(空)'}: {m[1]} 篇" for m in ctx.get("kb_top_modules", []))
    gt = "\n".join(f"    {t[0]}: {t[1]} 条" for t in ctx.get("gap_topics", []))
    tm = ctx.get("training_metrics", [])
    tm_str = "\n".join(f"    n={m.get('n')} avg_score={m.get('avg_score')} pass_rate={m.get('pass_rate')}" for m in tm) or "（无数据）"
    return f"""
【真实基线数据 — 本次辩论必须引用这些数字，不得编造】

一、回复采纳率
  当前活跃采纳率：{ctx.get('adoption_rate','?')}（目标 15%）
  活跃总量：{ctx.get('live_total',0)} 条  采纳：{ctx.get('live_adopted',0)}  修改：{ctx.get('live_modified',0)}

二、KB 知识库结构
  总文档数：{ctx.get('kb_docs',0)}  总 Chunk 数：{ctx.get('kb_chunks',0)}
  按 l1_module 前10（这是 KB 实际偏向的领域）：
{km}

三、KB 缺口分析（reply_diff_analyzer 产出 130 行 kb_gaps.jsonl）
  总缺口数：{ctx.get('gap_total',0)}  其中 AI 明示「暂未查到」：{ctx.get('gap_kb_miss',0)} 条
  缺口 Topic 分布（前10）：
{gt}

四、训练近况（最近 3 次 training_metrics）
{tm_str}

五、当前回复风格规则（reply_style_rules.md 节选）
{ctx.get('style_rules_summary','')[:800]}
"""


# ─── Agent role prompts ───────────────────────────────────────────────────────

ROLES = {
    "proposer": {
        "name": "Proposer（提案者）",
        "system": textwrap.dedent("""\
            你是 PRD产品主导 Agent。你的任务是为「模块感知智能回复」提出具体可落地的三层优化方案，
            并用真实数据为每一层方案提供 1 条以上证据支撑。
            要求：立场明确、有证据支撑、聚焦实现路径、敢于承诺具体指标。
            禁止：空泛表述、未引用数字的结论、超过上下文数据范围的推断。"""),
    },
    "critic": {
        "name": "Critic（质疑者）",
        "system": textwrap.dedent("""\
            你是 PRD产品分析 Agent。你的任务是严格质疑「模块感知智能回复」提案的每一个薄弱点。
            质疑方向：模块归属信号不稳定（Jira customfield_15805 可能为空/错）、
            样本量不足（224条活跃样本分散到94个模块≈每模块2.4条）、
            维护成本（per-module 风格文件谁来写谁来维护）、
            KB 分布偏差本身是否是不同问题（技术vs业务内容类型不同）。
            要求：每条质疑必须引用具体数据，提出可证伪条件（什么情况下你会同意方案）。
            禁止：纯否定而不给可证伪条件，无数据支持的直觉判断。"""),
    },
    "ux": {
        "name": "UX Advocate（体验代言）",
        "system": textwrap.dedent("""\
            你是 UX设计主导 Agent。你代表最终用户——客服工程师。
            你的任务是评估：当回复与工单模块不匹配时，客服工程师的实际损失是什么；
            以及：术语切换（工作流→审批流→流程引擎）的认知成本有多高。
            要求：给出 3 类具体失败场景（结合 kb_gaps 里的真实 topic），
            提出「最低可接受体验标准」（minimum bar），以及什么改动会让体验变差。
            禁止：只谈理论体验；必须结合真实 topic 数据举例。"""),
    },
    "judge": {
        "name": "Judge（数据评判）",
        "system": textwrap.dedent("""\
            你是 Darwin 进化 Agent 的评估模块。你对三个候选方案打分（0.0-1.0 per维度），
            不偏袒任何立场，只看数据和逻辑。
            四个评判维度：可行性（数据/代码已具备几成）、预期收益（采纳率能改善多少）、
            实施成本（工程量+维护量）、风险（如果方案失效的损失）。
            要求：给出每个维度的分数 + 1 句理由（引用具体数据），给出加权总分和排名。
            候选方案：
              方案A（最小可行）= 仅开 KB category 过滤，不改 prompt/样例
              方案B（中等）= KB 过滤 + per-module prompt 风格
              方案C（完整）= KB 过滤 + per-module 风格 + 样例按模块过滤 + Skill+API
            禁止：主观判断代替数据推断，分数没有理由。"""),
    },
    "verifier": {
        "name": "Verifier（裁决者）",
        "system": textwrap.dedent("""\
            你是 Darwin 进化 Agent。你综合三轮辩论和 Judge 打分，做出最终裁决。
            你的输出是正式的 PRD 决议文档，包含：
            1. 最终选择的方案及理由
            2. 价值（3条，有数据支撑）
            3. 先进性（3点，与业界对比）
            4. 使用场景（4个，真实）
            5. 评判方法（离线+在线）
            6. 关键指标（当前基线+目标值+测量方法，表格形式）
            7. 预判效果（区间估算，附假设条件）
            8. 回滚条件
            9. 不做清单（本期边界）
            要求：所有数字必须从基线数据推算，每条推算写出假设。
            禁止：编造数字、无假设的预测、超过 6 周的实施计划。"""),
    },
}


# ─── Debate rounds ────────────────────────────────────────────────────────────

def _r1_prompt(role: str, ctx_str: str) -> str:
    briefs = {
        "proposer": "提出「模块感知智能回复」三层方案：(A) KB category 过滤 (B) per-module prompt 风格 (C) 样例按模块过滤。每层必须引用基线数据写出为何该层能带来改善，预计各层单独贡献采纳率提升幅度，并说明实现路径（具体代码改动点）。",
        "critic":   "评估提案的三大薄弱点：(1) 模块信号可靠性（Jira field 缺失率/错误率估计）(2) 每模块样本量（224÷94 模块）的统计意义 (3) per-module 风格文件的维护成本 ROI。每条给出「可证伪条件」——达到什么阈值你会同意方案推进。",
        "ux":       "描述 3 类真实失败场景：从 kb_gaps 的 topic 中选 3 个，描述「AI回复说了工作流，客服工程师实际在处理费控审批矩阵」时的认知损耗、修改时间、错误率。给出 minimum bar（最低可接受标准，量化）。",
        "judge":    "对三个候选方案（A/B/C）在4个维度评分。A=仅KB过滤，B=KB过滤+风格，C=完整方案(KB+风格+样例+Skill+API)。每个维度 0.0-1.0 打分并给出 1 句理由（引用数据）。计算加权总分（可行性0.3/收益0.3/成本0.2/风险0.2），输出排名。",
        "verifier": "监听 Round 1 其他角色的立场，记录每角色的核心主张和最大疑虑，供 Round 3 综合裁决使用。本轮输出：(1) 各角色核心立场摘要 (2) Round 2 需要解决的 3 个关键分歧。",
    }
    return f"""{ctx_str}

[Round 1 — 各自亮证]
你是 {ROLES[role]['name']}。请基于以上真实数据写出你的立场书（约 600-800 字）。

{briefs[role]}

格式：纯文字，分段落，引用数据用「（来源：XXX）」标注。"""


def _r2_prompt(role: str, r1_outputs: dict[str, str], ctx_str: str) -> str:
    r1_proposer = r1_outputs.get("proposer", "")[:1500]
    r1_critic   = r1_outputs.get("critic", "")[:1500]
    r1_ux       = r1_outputs.get("ux", "")[:1000]
    r1_judge    = r1_outputs.get("judge", "")[:800]

    if role == "critic":
        return f"""{ctx_str}

[Round 2 — 对抗交锋]
Proposer 的 Round 1 立场：
{r1_proposer}

你是 {ROLES[role]['name']}。请逐条反驳 Proposer 的三层方案中你认为最薄弱的 2-3 个论据。
要求：每条反驳必须（1）指出 Proposer 的具体论断 (2) 提供反证数据或逻辑漏洞 (3) 给出可让你改变立场的阈值。
最后一段：如果只做最小可行（方案A = 仅KB过滤），你接受吗？给出条件。"""

    if role == "ux":
        return f"""{ctx_str}

[Round 2 — 对抗交锋]
Proposer Round 1：{r1_proposer[:800]}
Critic Round 1：{r1_critic[:800]}

你是 {ROLES[role]['name']}。
任务：给出「客服工程师读到 module-mismatch 回复时」的 3 类具体失败场景，每类必须：
  - 用一个 kb_gaps 里的真实 topic（如「费控审批矩阵」「工作流配置」）
  - 描述 AI 回复内容的错位（如"说了工作流步骤，实际是费控矩阵配置问题"）
  - 估算客服工程师额外耗时（分钟）
  - 说明这种错位对最终用户（工单提交者）的影响
最后：输出你的 Minimum Bar（最低可接受标准，量化）。"""

    if role == "judge":
        return f"""{ctx_str}

[Round 2 — 中场打分]
Proposer 立场：{r1_proposer[:800]}
Critic 立场：{r1_critic[:800]}
UX 立场：{r1_ux[:600]}

你是 {ROLES[role]['name']}。给出中场评分（方案A/B/C，4维度）：
  A = 仅 KB category 过滤
  B = KB 过滤 + per-module prompt 风格
  C = KB 过滤 + per-module 风格 + 样例按模块 + Skill+API

每个方案每个维度给出：分数(0.0-1.0) + 理由（引用数据，1 句）
维度：可行性 / 预期收益 / 实施成本 / 回归风险
计算加权总分 (0.3/0.3/0.2/0.2)，输出当前阶段排名。"""

    if role == "proposer":
        return f"""{ctx_str}

[Round 2 — 回应质疑]
Critic 的质疑：{r1_critic[:1200]}

你是 {ROLES[role]['name']}。逐条回应 Critic 最强的 2 条质疑：
1. 针对「模块信号可靠性」：Jira customfield_15805 在实际工单中的覆盖率，和你方案的降级策略
2. 针对「每模块样本量 224/94 ≈ 2.4 条」：为什么你的方案在样本稀少时仍有效（或只推荐完整方案给高样本模块）
最后：如果只批准方案A（仅KB过滤），你认为能获得完整方案多少比例的收益？给出数字。"""

    # verifier in round 2
    return f"""{ctx_str}

[Round 2 — 分歧追踪]
R1 Proposer：{r1_proposer[:800]}
R1 Critic：{r1_critic[:800]}
R1 UX：{r1_ux[:600]}
R1 Judge 分数：{r1_judge[:600]}

你是 {ROLES[role]['name']}。整理三个核心未解分歧，格式：
分歧1：[主题] Proposer 主张 vs Critic 主张，Judge 倾向哪边？
分歧2：……
分歧3：……
Round 3 裁决方向：你将如何权衡这三个分歧？"""


def _r3_prompt(role: str, r1: dict, r2: dict, ctx_str: str) -> str:
    r1p = r1.get("proposer","")[:1000]
    r1c = r1.get("critic","")[:1000]
    r2c = r2.get("critic","")[:1000]
    r2p = r2.get("proposer","")[:1000]
    r2u = r2.get("ux","")[:800]
    r2j = r2.get("judge","")[:800]
    r2v = r2.get("verifier","")[:600]

    if role == "proposer":
        return f"""{ctx_str}

[Round 3 — 最终陈述]
Critic Round 2 质疑：{r2c[:800]}

你是 {ROLES[role]['name']}。最终陈述：
针对 Critic 「per-module 样例每模块 2.4 条不够用」的质疑，提出你修正后的方案：
  - 哪些模块有足够样本（用真实的 kb_gaps topic 数量作参考），先启用
  - 哪些模块样本不足，降级到全局
  - 承诺：方案上线 4 周后，如果整体采纳率未超过 4.5%，你同意回滚。给出数字。
（限 400 字）"""

    if role == "ux":
        return f"""{ctx_str}

[Round 3 — Minimum Bar 确认]
你是 {ROLES[role]['name']}。在本轮请确认你的最低可接受标准（Minimum Bar）：
  - 术语错位率（「工作流」出现在非工作流模块回复中的比例）降低多少，才算体验达标？
  - 客服额外修改时间（分钟/条）从多少降到多少？
  - 哪 1 个改动是你的红线（如果没有这个改动，你反对整个方案）？
请给出具体数字（可以是估算范围），并说明如何测量。（限 300 字）"""

    if role == "judge":
        return f"""{ctx_str}

[Round 3 — 最终评分]
全部辩论摘要：
R1 Proposer：{r1p[:600]}
R1 Critic：{r1c[:600]}
R2 Critic 反驳：{r2c[:600]}
R2 Proposer 回应：{r2p[:600]}
R2 UX 失败场景：{r2u[:500]}
中场分数：{r2j[:400]}

你是 {ROLES[role]['name']}。给出最终评分（A/B/C 四维度），推荐一个方案，
并说明附加条件（如：方案B可推进但需满足「前 8 周先跑方案A做基线」）。"""

    if role == "critic":
        return f"""{ctx_str}

[Round 3 — 最终立场]
Proposer Round 2 回应：{r2p[:1000]}

你是 {ROLES[role]['name']}。最终立场（限 300 字）：
  - Proposer 回应中哪条你接受了？哪条你仍然不接受？
  - 你能接受的最低方案是什么？附条件。"""

    # verifier: final verdict — full PRD
    all_text = f"""
R1-Proposer: {r1p}

R1-Critic: {r1c}

R2-Critic反驳: {r2c}

R2-Proposer回应: {r2p}

R2-UX失败场景: {r2u}

R2-Judge中场分: {r2j}

R2-Verifier分歧: {r2v}

R3-Judge最终分: {r2.get('judge_r3','（待填）')[:600]}

R3-Proposer最终: {r2.get('proposer_r3','（待填）')[:600]}

R3-UX最低标准: {r2.get('ux_r3','（待填）')[:400]}

R3-Critic最终: {r2.get('critic_r3','（待填）')[:400]}
"""
    return f"""{ctx_str}

[Round 3 — Verifier 最终裁决 + PRD 决议]

以下是三轮辩论全记录：
{all_text}

你是 {ROLES[role]['name']}（最终裁决者）。
请输出正式 PRD 决议文档，用 Markdown，包含以下章节（必须完整，不得省略）：

# 模块感知智能回复 PRD 决议

## 一、最终方案选择
（选 A/B/C 或组合，给出理由，引用 Judge 分数和具体辩论论据）

## 二、方案价值（3 条，每条附数据推算）

## 三、先进性（3 点，结合业界 multi-agent debate / Constitutional AI / RAG-with-metadata 对比）

## 四、使用场景（4 个，结合真实 topic 和用户类型）

## 五、评判方法
- 离线双盲评分：方法、样本、维度
- 在线指标追踪：周期、数据源

## 六、关键指标表
| 指标 | 当前基线 | 目标值 | 测量位置 |
（至少 5 行，用真实基线数字）

## 七、预判效果
（分模块：头部高覆盖模块 vs 尾部低覆盖模块，给区间估算，附假设）

## 八、回滚条件
（具体：什么指标触发 revert 哪个组件）

## 九、本期不做
（明确边界，避免范围蔓延）

## 十、Skill + API 设计摘要
（/smart-reply-by-module 调用形式 + /api/reply/generate-by-module + /api/reply/module-coverage 端点说明）
"""


# ─── Orchestrator main ────────────────────────────────────────────────────────

def run_debate(rounds: int = 3, dry_run: bool = False) -> Path:
    DEBATE_DIR.mkdir(parents=True, exist_ok=True)
    PRD_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[Debate] 加载真实上下文数据...")
    ctx     = _load_context()
    ctx_str = _format_context(ctx)

    if dry_run:
        print("[Debate] dry-run 模式：跳过 LLM 调用，写占位符文件")
        for role in ROLES:
            for rnd in range(1, rounds + 1):
                path = DEBATE_DIR / f"round{rnd}_{role}.md"
                path.write_text(f"# [{ROLES[role]['name']}] Round {rnd} — dry-run 占位符\n\n{ctx_str[:300]}\n",
                                encoding="utf-8")
        prd = PRD_DIR / "prd_module_aware_reply.md"
        prd.write_text("# PRD dry-run 占位符\n", encoding="utf-8")
        return prd

    def _save(role: str, rnd: int, text: str) -> Path:
        path = DEBATE_DIR / f"round{rnd}_{role}.md"
        path.write_text(
            f"# [{ROLES[role]['name']}] Round {rnd}\n\n*生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n{text}\n",
            encoding="utf-8",
        )
        print(f"  ✓ {path.name} ({len(text)} chars)")
        return path

    # ── Round 1 ──────────────────────────────────────────────────────────────
    print("\n[Round 1] 各自亮证（5 Agent 并行）...")
    r1: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {
            ex.submit(_call_chain, ROLES[role]["system"],
                      _r1_prompt(role, ctx_str), 2500, role): role
            for role in ROLES
        }
        for fut in as_completed(futs):
            role = futs[fut]
            try:
                text = fut.result()
            except Exception as e:
                text = f"[异常: {e}]"
            r1[role] = text
            _save(role, 1, text)
    print(f"[Round 1] 完成")

    if rounds < 2:
        return DEBATE_DIR / "round1_verifier.md"

    # ── Round 2 ──────────────────────────────────────────────────────────────
    print("\n[Round 2] 对抗交锋（5 Agent 并行）...")
    r2: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs2 = {
            ex.submit(_call_chain, ROLES[role]["system"],
                      _r2_prompt(role, r1, ctx_str), 2500, role): role
            for role in ROLES
        }
        for fut in as_completed(futs2):
            role = futs2[fut]
            try:
                text = fut.result()
            except Exception as e:
                text = f"[异常: {e}]"
            r2[role] = text
            _save(role, 2, text)
    print(f"[Round 2] 完成")

    if rounds < 3:
        return DEBATE_DIR / "round2_verifier.md"

    # ── Round 3 ──────────────────────────────────────────────────────────────
    print("\n[Round 3] 收敛裁决（proposer/ux/judge/critic 先并行，verifier 最后）...")

    r3_roles = ["proposer", "ux", "judge", "critic"]
    r3: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs3 = {
            ex.submit(_call_chain, ROLES[role]["system"],
                      _r3_prompt(role, r1, r2, ctx_str), 2000, role): role
            for role in r3_roles
        }
        for fut in as_completed(futs3):
            role = futs3[fut]
            try:
                text = fut.result()
            except Exception as e:
                text = f"[异常: {e}]"
            r3[role] = text
            _save(role, 3, text)

    # Inject R3 outputs into r2 dict for verifier (it reads from r2)
    r2["judge_r3"]    = r3.get("judge", "")
    r2["proposer_r3"] = r3.get("proposer", "")
    r2["ux_r3"]       = r3.get("ux", "")
    r2["critic_r3"]   = r3.get("critic", "")

    print("  → Verifier 综合裁决（生成 PRD）...")
    prd_text = _call_chain(ROLES["verifier"]["system"],
                           _r3_prompt("verifier", r1, r2, ctx_str),
                           max_tokens=3500, role="verifier")
    _save("verifier", 3, prd_text)

    prd_path = PRD_DIR / "prd_module_aware_reply.md"
    prd_path.write_text(
        f"<!-- 由 debate_orchestrator.py 自动生成 {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n"
        f"<!-- 三轮辩论 × 5 Agent，基于真实基线数据 -->\n\n"
        + prd_text,
        encoding="utf-8",
    )
    print(f"\n[Debate] 完成！PRD 决议：{prd_path}")
    return prd_path


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PRD Agent Team 多轮对抗辩论编排器")
    parser.add_argument("--topic", default="module-aware reply")
    parser.add_argument("--rounds", type=int, default=3, choices=[1, 2, 3])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = time.time()
    prd = run_debate(rounds=args.rounds, dry_run=args.dry_run)
    elapsed = round(time.time() - start)
    print(f"\n总耗时：{elapsed}s  输出：{prd}")
