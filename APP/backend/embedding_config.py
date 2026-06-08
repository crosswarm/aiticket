"""嵌入模型配置单一真相源（Phase 3 中心化，消除 4 处硬编码漂移风险）。

config/embedding.json 控制模型；默认 MiniLM（issues/reply 多语言 384维、kb 英文 384维），
保持现网行为。切 bge(768维) 由 P3.4 cutover 做。env AITICKET_EMBED_MODEL 可覆盖便于 A/B。

⚠️ 维度一致性：任何集合重建/查询必须用同一模型，384/768 混用会导致 Chroma 维度不匹配崩溃。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
_CONFIG_PATH = _BACKEND / "config" / "embedding.json"

_DEFAULT: dict = {
    "model_name": "paraphrase-multilingual-MiniLM-L12-v2",
    "kb_model_name": "all-MiniLM-L6-v2",
    "dim": 384,
    "query_instruction": "",
    "normalize": False,
    # A2 cutover：serving 集合后缀。""=v1（MiniLM）；"_v2"=bge 重建集合。
    # env AITICKET_CHROMA_COLLECTION_SUFFIX 优先于本字段。
    "collection_suffix": "",
}


def load_embedding_config() -> dict:
    """读 config/embedding.json，缺失/损坏时回退默认。env AITICKET_EMBED_MODEL 覆盖 model_name。"""
    cfg = dict(_DEFAULT)
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        # 只取已知键，忽略 _note/_bge_target 等文档字段
        for k in _DEFAULT:
            if k in raw and not k.startswith("_"):
                cfg[k] = raw[k]
    except Exception:
        pass
    env_model = os.environ.get("AITICKET_EMBED_MODEL")
    if env_model:
        # kb 与主模型同名时（配置为联动）一并切换，避免 A/B 时 KB 仍用旧模型导致维度/语义错位；
        # 若用户显式把 kb_model_name 配成不同模型则尊重其拆分，不覆盖。
        if cfg.get("kb_model_name") == cfg.get("model_name"):
            cfg["kb_model_name"] = env_model
        cfg["model_name"] = env_model
    return cfg


def get_embedding_model_name() -> str:
    """主嵌入模型（issues / reply_examples / 负配对）。"""
    return load_embedding_config()["model_name"]


def get_kb_embedding_model_name() -> str:
    """KB 混合检索专用模型（独立默认，便于其重建故事单独切换）。"""
    return load_embedding_config().get("kb_model_name", _DEFAULT["kb_model_name"])


def get_query_instruction() -> str:
    """检索 query 侧前缀（bge 非对称检索用；MiniLM 为空 → 无操作）。doc 侧不加。"""
    return load_embedding_config().get("query_instruction", "")


def get_embedding_dim() -> int:
    return int(load_embedding_config().get("dim", 384))


def get_collection_suffix() -> str:
    """A2 serving 集合后缀（""=v1/MiniLM，"_v2"=bge）。env 优先于 config。"""
    env = os.environ.get("AITICKET_CHROMA_COLLECTION_SUFFIX")
    if env is not None:
        return env
    return load_embedding_config().get("collection_suffix", "") or ""
