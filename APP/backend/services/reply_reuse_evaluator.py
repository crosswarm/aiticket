"""Gate 3 历史复用评估：对 reply_examples 候选计算复合分，返回最佳候选及复用层级。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_NEGATIVE_PAIRS_PATH = _PROJECT_ROOT / "data" / "badcase_negative_pairs.jsonl"

# 负配对压制余弦阈值（默认 bge 标定 0.75）
_NEG_PAIR_SUPPRESS_COS = 0.75


# ── 公有云客开违规检测（Layer 3 硬隔离 + Layer 1 direct 出口网共用）──────────────
# 公有云（yonsuite）客开受限：私有化工单的"建议走客开"回复不得复用到公有云工单。
# 只匹配肯定性建议句式，不匹配"不支持客开/无法客开"等正确否定句，避免误伤。
_CUSTOM_DEV_SUGGEST_RE = re.compile(
    r"(联系|找|咨询|建议|可以|可考虑|需要|请|通过|安排).{0,10}(客开|客户化开发|二次开发|定制开发)"
    r"|私有化.{0,12}(客开|定制开发|二次开发|脚本)"
    r"|(客户化开发|二次开发|客开老师).{0,8}(实现|处理|解决|配置|协助)"
)


def reply_suggests_custom_dev(text: str) -> bool:
    """回复是否包含'建议走客开'的肯定性话术（不含'不支持客开'等否定句）。"""
    if not text:
        return False
    return bool(_CUSTOM_DEV_SUGGEST_RE.search(str(text)))


def is_public_cloud(deploy_mode: str) -> bool:
    """部署模式是否为公有云（yonsuite）——客开受限。"""
    if not deploy_mode:
        return False
    dm = str(deploy_mode)
    return "公有云" in dm or "yonsuite" in dm.lower()


# ── 负配对缓存（mtime 驱动，热更新）────────────────────────────────────────────
_neg_pairs_cache: dict[str, list[list[float]]] = {}  # wrong_ticket -> [query_embeddings]
_neg_pairs_mtime: float = 0.0


def _load_negative_pairs() -> dict[str, list[list[float]]]:
    """加载负配对文件，按 mtime 缓存，返回 wrong_ticket→[embeddings] 映射。"""
    global _neg_pairs_cache, _neg_pairs_mtime
    if not _NEGATIVE_PAIRS_PATH.exists():
        return {}
    mtime = _NEGATIVE_PAIRS_PATH.stat().st_mtime
    if mtime == _neg_pairs_mtime and _neg_pairs_cache:
        return _neg_pairs_cache
    pairs: dict[str, list[list[float]]] = {}
    for line in _NEGATIVE_PAIRS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            wt = rec.get("wrong_ticket", "")
            emb = rec.get("query_embedding")
            if wt and emb:
                pairs.setdefault(wt, []).append(emb)
        except Exception:
            pass
    _neg_pairs_cache = pairs
    _neg_pairs_mtime = mtime
    return pairs


def _cosine(a: list, b: list) -> float:
    """纯 Python 余弦相似度（bge 768 维约 1ms，仅负配对命中时才调用）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


_ef_singleton = None


def _embed_text(text: str) -> list[float] | None:
    """将文本嵌入为查询向量（惰性加载，仅负配对命中时调用）。

    Phase3：模型名读 embedding_config 单一真相源（消除第 5 处 MiniLM 硬编码）。
    本函数嵌入的是负配对的 query 侧文本 → bge 非对称检索须加 query_instruction 前缀，
    并与 doc 侧统一 normalize（否则余弦阈值物理意义错位）。
    """
    global _ef_singleton
    if _ef_singleton is None:
        try:
            from chromadb.utils import embedding_functions
            from embedding_config import get_embedding_model_name
            _ef_singleton = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=get_embedding_model_name()
            )
        except Exception as e:
            logger.warning("[NegativePair] 嵌入模型加载失败: %s", e)
            return None
    try:
        from embedding_config import get_query_instruction
        prefix = get_query_instruction() or ""
    except Exception:
        prefix = ""
    try:
        vec = list(_ef_singleton([prefix + text])[0])
    except Exception as e:
        logger.warning("[NegativePair] embed 失败: %s", e)
        return None
    # 与 doc 侧统一 L2 归一化（bge normalize=True；MiniLM 下也归一不改变余弦结果）
    try:
        norm = sum(x * x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]
    except Exception:
        pass
    return vec

# 权重（与 config/reply_gates.yaml score_weights 对应）
_DEFAULT_WEIGHTS = {
    "similarity": 0.475,
    "adoption_signal": 0.375,
    "recency": 0.0,
    "version_match": 0.15,
}


@dataclass
class ReuseCandidate:
    example: dict
    composite_score: float
    tier: str   # "direct" | "llm_blend" | "skip"
    score_breakdown: dict


def evaluate_reuse(
    reply_examples: list[dict],
    current_product_version: str = "",
    current_module: str = "",
    current_issue_key: str = "",
    current_query_text: str = "",
    current_deploy_mode: str = "",
) -> Optional[ReuseCandidate]:
    """
    评估 reply_examples 候选，返回最佳候选 (ReuseCandidate) 或 None。
    None 表示：无候选、门关闭、或最高分 < skip 阈值。

    current_query_text: 当前工单查询文本，用于负配对 cosine 泛化压制。
    """
    if not reply_examples:
        return None

    cfg = _load_gate_config()
    if not cfg.get("enabled", False):
        return None

    fix_v2 = cfg.get("fix_v2", False)

    # 权重：fix_v2=True 且 profile=v2 时用 score_weights_v2，否则沿用 legacy
    if fix_v2 and cfg.get("score_weights_profile") == "v2":
        weights = {**_DEFAULT_WEIGHTS, **cfg.get("score_weights_v2", {})}
    else:
        weights = {**_DEFAULT_WEIGHTS, **cfg.get("score_weights", {})}

    composite_threshold = float(cfg.get("composite_threshold", 0.85))
    llm_blend_min = float(cfg.get("llm_blend_min", 0.60))
    min_sim_blend = float(cfg.get("min_similarity_blend", 0.70))
    min_sim_direct = float(cfg.get("min_similarity_direct", 0.82))

    # fix_v2: 独立召回地板（不复用 min_sim_blend，避免 tier 和召回两个阈值耦合）
    min_sim_recall = float(cfg.get("min_similarity_recall", 0.55)) if fix_v2 else 0.0

    # fix_v2: signal_source 权重表（imported_adopted 降权到 0.3，不排除）
    adoption_weights_table = cfg.get("adoption_weights", {}) if fix_v2 else {}

    # 负配对降权：wrong_ticket → 已知 bad-query embeddings
    neg_pairs = _load_negative_pairs()
    _lazy_emb: list = [None]  # 当前 query 的 embedding，惰性计算

    def _get_cur_emb() -> list[float] | None:
        if _lazy_emb[0] is None and current_query_text:
            _lazy_emb[0] = _embed_text(current_query_text)
        return _lazy_emb[0]

    _cur_is_cloud = is_public_cloud(current_deploy_mode)

    best: Optional[ReuseCandidate] = None
    for ex in reply_examples:
        ex_key = ex.get("issue_key", "")

        # Layer 3 公有云客开硬隔离：当前是公有云工单 且 候选回复含"建议走客开"话术
        # → 直接跳过，不进候选池（根治：私有化客开回复永不被公有云工单复用）
        if _cur_is_cloud and reply_suggests_custom_dev(ex.get("reply", "")):
            logger.info("[CloudPolicy] %s 公有云工单跳过含客开建议的复用候选 %s",
                        current_issue_key or "?", ex_key)
            continue

        # 负配对压制：仅当候选在已知 wrong_ticket 集合中，才惰性计算 cosine
        if ex_key and ex_key in neg_pairs:
            cur_emb = _get_cur_emb()
            if cur_emb is not None:
                bad_embeddings = neg_pairs[ex_key]
                max_cos = max((_cosine(cur_emb, be) for be in bad_embeddings), default=0.0)
                # bge 标定 0.75：bge cosine 压缩，强相关~0.78、误召回~0.46
                # → 0.75 既能命中"几乎同一坏 query"又不误伤普通相关 query
                if max_cos > _NEG_PAIR_SUPPRESS_COS:
                    logger.info("[NegativePair] %s↔%s suppressed (cos=%.3f)",
                                current_issue_key or "?", ex_key, max_cos)
                    continue  # 跳过此候选，不进入评分

        score_bd = _score_example(
            ex, current_product_version, current_module, weights,
            adoption_weights_table=adoption_weights_table,
        )
        composite = score_bd["composite"]
        sim = score_bd["similarity"]
        if best is None or composite > best.composite_score:
            # fix_v2: sim 召回地板独立于 tier 地板，两者均须满足
            if composite >= llm_blend_min and sim >= min_sim_recall:
                best = ReuseCandidate(
                    example=ex,
                    composite_score=round(composite, 4),
                    tier=_assign_tier(composite, ex, composite_threshold, llm_blend_min,
                                      sim, min_sim_blend, min_sim_direct),
                    score_breakdown=score_bd,
                )

    return best


def _assign_tier(
    composite: float,
    example: dict,
    composite_threshold: float,
    llm_blend_min: float,
    sim: float = 1.0,
    min_sim_blend: float = 0.70,
    min_sim_direct: float = 0.82,
) -> str:
    # 相似度地板：sim 不够则强制 skip，无论 composite 多高
    if sim < min_sim_blend:
        return "skip"
    if composite >= composite_threshold and example.get("adopted") and sim >= min_sim_direct:
        return "direct"
    if composite >= llm_blend_min:
        return "llm_blend"
    return "skip"


def _score_example(
    ex: dict,
    current_version: str,
    current_module: str,
    weights: dict,
    adoption_weights_table: dict | None = None,
) -> dict:
    # 1. 相似度：优先用 sim_score（归一化的 [0,1] 值），fallback 到 score（兼容旧数据）
    # reply_trainer 返回的 score 已乘以排序权重（最高 ≈2.1），sim_score 是原始值
    sim = min(max(float(ex.get("sim_score", ex.get("score", 0.0))), 0.0), 1.0)

    # 2. 采纳信号：fix_v2 时查 signal_source 权重表，否则用旧逻辑兜底
    signal_source = ex.get("signal_source", "")
    if adoption_weights_table and signal_source and signal_source in adoption_weights_table:
        adoption = float(adoption_weights_table[signal_source])
    elif ex.get("adopted"):
        adoption = 1.0
    elif ex.get("is_modified"):
        adoption = 0.6
    else:
        adoption = 0.0

    # 3. 近期性：优先用真实时间戳（外部 created_at / Jira created），否则回退 issue_key 数字推断
    recency = _score_recency(ex.get("issue_key", ""), ex.get("created_at", ""))

    # 4. 版本匹配
    version_match = _score_version_match(ex.get("reply", ""), current_version)

    composite = (
        sim * weights.get("similarity", 0.40)
        + adoption * weights.get("adoption_signal", 0.30)
        + recency * weights.get("recency", 0.15)
        + version_match * weights.get("version_match", 0.15)
    )

    return {
        "composite": composite,
        "similarity": round(sim, 4),
        "adoption_signal": round(adoption, 4),
        "recency": round(recency, 4),
        "version_match": round(version_match, 4),
    }


def _score_recency(issue_key: str, created_at: str = "") -> float:
    """近期性评分。

    优先用真实时间戳 created_at（外部工单无 XXX-数字 编号，issue_key 尾数无意义）：
    越近越高（30 天内≈1.0，线性衰减到 2 年外 0.3）。无时间戳时回退 issue_key 尾部数字推断。
    """
    if created_at:
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
            if age_days <= 30:
                return 1.0
            # 30 天 → 730 天 (2 年) 线性从 1.0 衰减到 0.3
            recency = 1.0 - (age_days - 30) / 700.0 * 0.7
            return round(max(min(recency, 1.0), 0.3), 4)
        except (ValueError, TypeError):
            pass  # 时间戳解析失败 → 回退编号推断

    m = re.search(r'(\d+)$', issue_key)
    if not m:
        return 0.5
    num = int(m.group(1))
    # 假设当前最大编号约 50000；数字越大近期性越高，最低 0.3
    recency = min(num / 50000.0, 1.0) * 0.7 + 0.3
    return round(min(recency, 1.0), 4)


def _score_version_match(reply_text: str, current_version: str) -> float:
    """
    检测回复中是否提及版本号，并与当前工单版本比较。
    - 当前版本未知 → 0.5（中性）
    - 回复无版本提及 → 0.7（较安全）
    - 版本提及且主版本匹配 → 1.0
    - 版本提及但不匹配 → 0.2
    """
    if not current_version:
        return 0.5
    version_mentions = re.findall(r'\d+\.\d+[\.\d]*', reply_text)
    if not version_mentions:
        return 0.7
    # 比较主版本（前两段）
    current_major = ".".join(current_version.split(".")[:2])
    for v in version_mentions:
        major = ".".join(v.split(".")[:2])
        if major == current_major:
            return 1.0
    return 0.2


def _load_gate_config() -> dict:
    try:
        path = _PROJECT_ROOT / "config" / "reply_gates.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return raw.get("gates", {}).get("reuse", {})
    except Exception:
        return {}
