"""
Chroma Vector Database Manager
管理工单向量存储、AI分析结果缓存、相似度关系
"""

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
import json
import hashlib
import logging
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import asdict
import numpy as np
import os

logger = logging.getLogger(__name__)


def _apply_query_instruction(text: str) -> str:
    """检索 query 侧前缀（bge 非对称检索；MiniLM 下前缀为空 → 原样返回）。

    Phase3 F2：doc 侧不加前缀（由 EF __call__ 处理），仅 query 文本在此预拼，
    因 Chroma 1.5.5 单一 __call__ 无法区分 doc/query。
    """
    try:
        from embedding_config import get_query_instruction
        prefix = get_query_instruction() or ""
    except Exception:
        prefix = ""
    return f"{prefix}{text}" if prefix else text


class _NormalizingSTEmbeddingFunction(embedding_functions.EmbeddingFunction):
    """SentenceTransformer EF，统一控制 L2 归一化（bge 非对称检索一致性）。

    Phase3 F2：
    - doc 侧（Chroma 对 .add() 与 .query(query_texts=...) 都走本 __call__）不加任何前缀；
    - query 侧的 bge 非对称前缀由调用方在 query 文本上预拼（见 search_* 方法），
      因为 Chroma 1.5.5 单一 __call__ 无法区分 doc/query；
    - normalize=True 时启用 normalize_embeddings，使余弦阈值物理意义与 doc 侧一致。
    """

    def __init__(self, model_name: str, normalize: bool = False) -> None:
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_name)
        self._normalize = bool(normalize)
        self._model_name = model_name

    def __call__(self, input):  # Chroma EF 协议：单参 input
        texts = list(input)
        embs = self._model.encode(
            texts,
            normalize_embeddings=self._normalize,
            show_progress_bar=False,
        )
        return embs.tolist()

    def name(self) -> str:
        return f"normalizing-st::{self._model_name}"


def get_embedding_function(api_key: str = None, allow_download: bool = True):
    """
    获取嵌入函数

    优先级：
    1. 如果有Gemini API key，使用Gemini嵌入
    2. 否则使用本地SentenceTransformer模型（无需API）

    Args:
        api_key: LLM API密钥
        allow_download: 是否允许下载模型（服务器部署时可设为False跳过下载）
    """
    # 如果不允许下载，直接返回None（使用已有向量）
    if not allow_download:
        print("[VectorStore] 跳过嵌入模型加载，使用已有向量数据")
        return None

    # 方案1: 本地模型（推荐，无需API）。模型名来自 embedding_config 单一真相源（Phase3 中心化）
    try:
        from embedding_config import (
            get_embedding_model_name,
            load_embedding_config,
        )
        _model = get_embedding_model_name()
        _cfg = load_embedding_config()
        _normalize = bool(_cfg.get("normalize", False))
        ef = _NormalizingSTEmbeddingFunction(
            model_name=_model,  # 默认 BAAI/bge-base-zh-v1.5(768维)，由 embedding_config 决定
            normalize=_normalize,
        )
        print(f"[VectorStore] 使用本地嵌入模型: {_model} (normalize={_normalize})")
        return ef
    except Exception as e:
        print(f"[VectorStore] 本地模型加载失败: {e}")

    # 方案2: 如果本地模型不可用，尝试Gemini
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)

            class GeminiEmbeddingFunction(embedding_functions.EmbeddingFunction):
                def __init__(self, client, model_name="models/text-embedding-004"):
                    self.client = client
                    self.model_name = model_name

                def __call__(self, texts):
                    result = self.client.models.embed_content(
                        model=self.model_name,
                        contents=texts,
                        config=genai.types.EmbedContentConfig(task_type="semantic_similarity")
                    )
                    return [e.values for e in result.embeddings]

            print("[VectorStore] 使用Gemini嵌入模型")
            return GeminiEmbeddingFunction(client)
        except Exception as e:
            print(f"[VectorStore] Gemini嵌入初始化失败: {e}")

    # 降级：使用Chroma默认嵌入
    print("[VectorStore] 使用Chroma默认嵌入函数")
    return None  # Chroma会使用默认的


class VectorStore:
    """
    Chroma向量数据库管理器
    集合1: issues - 存储工单原始内容向量
    集合2: analysis_cache - 存储AI分析结果
    集合3: similarity_graph - 存储工单相似度关系
    集合4: req_pool - 存储需求池
    集合5: query_cache - 查询结果缓存
    """

    def __init__(self, persist_directory: str = "./chroma_db", api_key: str = None, allow_download: bool = True):
        self.persist_directory = persist_directory

        # 初始化Chroma客户端
        from services.chroma_factory import get_chroma_client
        self.client = get_chroma_client(persist_path=persist_directory)

        # 检查是否应使用 Chroma 默认嵌入
        use_chroma_default = os.environ.get('USE_CHROMA_DEFAULT_EMBEDDING', 'false').lower() == 'true'

        # 获取嵌入函数（根据配置）
        if use_chroma_default:
            self.embedding_func = None  # Chroma会使用默认的
        else:
            self.embedding_func = get_embedding_function(api_key, allow_download)

        # 获取或创建集合
        self.issues_collection = self._get_or_create_collection("issues")
        self.analysis_collection = self._get_or_create_collection("analysis_cache")
        self.similarity_collection = self._get_or_create_collection("similarity_graph")
        self.req_pool_collection = self._get_or_create_collection("req_pool")
        self.req_clusters_collection = self._get_or_create_collection("req_clusters")

        # 初始化查询缓存集合
        self._init_query_cache_collection()

        print(f"[VectorStore] 初始化完成")
        print(f"  - 工单集合: {self._safe_collection_count(self.issues_collection)} 条")
        print(f"  - 分析缓存: {self._safe_collection_count(self.analysis_collection)} 条")
        print(f"  - 相似度图: {self._safe_collection_count(self.similarity_collection)} 条")
        print(f"  - 需求池: {self._safe_collection_count(self.req_pool_collection)} 条")
        print(f"  - 需求聚类: {self._safe_collection_count(self.req_clusters_collection)} 条")
        if self.query_cache:
            print(f"  - 查询缓存: {self._safe_collection_count(self.query_cache)} 条")
    
    # A2 cutover：仅这些集合有 bge 重建的 _v2 派生（其余如 similarity_graph/req_clusters/
    # query_cache 无 v2，不加后缀以免创建空集合破坏其功能）。
    _SUFFIXABLE_COLLECTIONS = frozenset({
        "issues", "analysis_cache", "req_pool",
    })

    def _resolve_collection_name(self, name: str) -> str:
        """A2 env 驱动集合后缀切换：serving 层读 _v2（cutover），清后缀即回滚 v1。

        后缀来源优先级：env AITICKET_CHROMA_COLLECTION_SUFFIX → embedding_config.collection_suffix。
        仅对 _SUFFIXABLE_COLLECTIONS 生效；切换=设后缀+重启，回滚=清后缀+重启，不 rename、不破坏 v1。
        """
        suffix = os.environ.get("AITICKET_CHROMA_COLLECTION_SUFFIX")
        if suffix is None:
            try:
                from embedding_config import load_embedding_config
                suffix = load_embedding_config().get("collection_suffix", "") or ""
            except Exception:
                suffix = ""
        if suffix and name in self._SUFFIXABLE_COLLECTIONS:
            return f"{name}{suffix}"
        return name

    def _get_or_create_collection(self, name: str):
        """获取或创建集合（A2：按 env/config 后缀解析到 _v2 集合）。

        Chroma 1.5.5 会把 EF 配置持久化进集合：已存在集合若用『不同』EF 打开会报
        『embedding function conflict』，用 create_collection 又因已存在报 already exists。
        故：① 先尝试带 EF get；② EF 冲突/已存在 → 不带 EF get（沿用存储配置，查询时由
        本类在 query_texts 上预拼前缀、向量已是目标模型所产）；③ 仍失败 → create。
        """
        resolved = self._resolve_collection_name(name)
        # ① 带 EF 直接 get（新建集合或 EF 一致时走这里）
        try:
            return self.client.get_collection(
                name=resolved,
                embedding_function=self.embedding_func,
            )
        except Exception:
            pass
        # ② 不带 EF get（集合已存在且持久化了 EF 配置 → 避免冲突）
        try:
            return self.client.get_collection(name=resolved)
        except Exception:
            pass
        # ③ 真不存在 → 创建
        try:
            return self.client.create_collection(
                name=resolved,
                embedding_function=self.embedding_func,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            # 并发/竞态下可能刚被建出 → 兜底再 get
            return self.client.get_collection(name=resolved)

    def _safe_collection_count(self, collection) -> int:
        try:
            return int(collection.count())
        except Exception as e:
            print(f"[VectorStore] 集合计数失败，按 0 处理: {e}")
            return 0

    def _init_query_cache_collection(self):
        """初始化查询缓存集合。

        走 _get_or_create_collection 三级容错（带 EF get → 不带 EF get → create），
        避免重启遇旧持久化 EF（如 384 MiniLM）与当前 bge EF 冲突时直接崩。
        query_cache 不在 _SUFFIXABLE_COLLECTIONS，解析为裸名 query_cache（无 _v2）。
        """
        try:
            self.query_cache = self._get_or_create_collection("query_cache")
            print("[VectorStore] query_cache集合初始化成功")
        except Exception as e:
            print(f"[VectorStore] query_cache集合初始化失败: {e}")
            self.query_cache = None
    
    # ==================== 工单向量操作 ====================
    
    def add_issue(self, issue_key: str, summary: str, description: str = "", 
                  metadata: Dict = None, embedding_text: str = None):
        """
        添加工单到向量库
        
        Args:
            issue_key: 工单编号 LCZX-12345
            summary: 标题
            description: 描述
            metadata: 元数据字段
            embedding_text: 用于生成向量的文本（默认使用 summary + description）
        """
        if embedding_text is None:
            embedding_text = f"{summary} {description}"[:2000]  # 限制长度
        
        # 生成内容哈希（用于检测变更）
        content_hash = hashlib.md5(embedding_text.encode()).hexdigest()[:16]
        
        doc_id = f"issue_{issue_key}"
        
        # 检查是否已存在
        try:
            existing = self.issues_collection.get(ids=[doc_id])
            if existing and existing['ids']:
                # 检查内容是否变更
                old_hash = existing['metadatas'][0].get('content_hash', '')
                if old_hash == content_hash:
                    return False  # 无变更，跳过
                # 内容变更，删除旧记录
                self.issues_collection.delete(ids=[doc_id])
        except Exception:
            pass
        
        # 构建元数据
        meta = {
            'issue_key': issue_key,
            'summary': summary[:500],  # Chroma元数据限制
            'content_hash': content_hash,
            'added_at': datetime.now().isoformat(),
            **{k: str(v)[:500] for k, v in (metadata or {}).items()}  # 确保可序列化
        }
        
        # 添加到集合
        self.issues_collection.add(
            ids=[doc_id],
            documents=[embedding_text],
            metadatas=[meta]
        )
        
        return True
    
    def search_similar_issues(self, query: str, top_k: int = 5,
                              min_score: float = 0.62) -> List[Dict]:  # 默认 bge 标定
        """
        语义搜索相似工单

        Returns:
            [{issue_key, summary, score, metadata}, ...]
        """
        if self.issues_collection.count() == 0:
            return []

        # 如果没有embedding函数（服务器离线模式），使用关键词搜索降级
        if self.embedding_func is None:
            return self._keyword_search(query, top_k)

        try:
            results = self.issues_collection.query(
                query_texts=[_apply_query_instruction(query)],
                n_results=min(top_k * 2, self.issues_collection.count()),  # 多取一些用于过滤
                include=['metadatas', 'distances', 'documents']
            )
        except Exception as e:
            print(f"[VectorStore] 语义搜索失败，降级到关键词搜索: {e}")
            return self._keyword_search(query, top_k)

        similar_issues = []
        for i in range(len(results['ids'][0])):
            distance = results['distances'][0][i]
            # Chroma使用余弦距离，转换为相似度分数
            score = 1 - distance  # 距离越小，相似度越高

            if score < min_score:
                continue

            metadata = results['metadatas'][0][i]
            similar_issues.append({
                'issue_key': metadata.get('issue_key'),
                'summary': metadata.get('summary', ''),
                'score': float(score),
                'document': results['documents'][0][i] if results.get('documents') else "",
                'metadata': metadata
            })

        # 按分数排序
        similar_issues.sort(key=lambda x: x['score'], reverse=True)
        result = similar_issues[:top_k]
        if result and random.random() < 0.10:
            top5 = [(r['issue_key'], round(r['score'], 3)) for r in result[:5]]
            logger.debug("[VectorStore] search_similar_issues: query=%r top5=%s filters=min_score=%.2f", query[:80], top5, min_score)
            import traceback as _tb
            _caller = "|".join(f"{f.filename.split('/')[-1]}:{f.lineno}" for f in _tb.extract_stack()[-4:-1])
            logger.debug("[DEPRECATED] VectorStore.search_similar_issues caller=%s", _caller)
        return result

    def _keyword_search(self, query: str, top_k: int = 5) -> List[Dict]:
        """
        关键词搜索（无embedding函数时的降级方案）

        使用BM25风格的关键词匹配算法，支持中英文
        """
        # 中文分词：按字符和空格分割，过滤单字符（保留中文单字）
        query_lower = query.lower()
        # 对于中文，按字符分割；对于英文，按空格分割
        import re
        # 提取2-4个字的子串作为搜索词，同时保留空格分隔的英文词
        query_words = set()

        # 添加空格分隔的词（英文）
        query_words.update(w for w in query_lower.split() if len(w) >= 2)

        # 添加2-4个字的子串（中文）
        for length in [4, 3, 2]:
            for i in range(len(query_lower) - length + 1):
                substr = query_lower[i:i + length]
                # 只添加包含中文字符的子串
                if any('\u4e00' <= c <= '\u9fff' for c in substr):
                    query_words.add(substr)

        if not query_words:
            # 如果没有提取到词，使用整个查询
            query_words = {query_lower}

        if not query_words:
            return []

        # 获取所有文档（分批获取避免内存问题）
        total_count = self.issues_collection.count()
        batch_size = 1000
        matches = []

        for offset in range(0, total_count, batch_size):
            batch_docs = self.issues_collection.get(
                limit=batch_size,
                offset=offset,
                include=['metadatas', 'documents']
            )

            for i, doc in enumerate(batch_docs.get('documents', [])):
                if not doc:
                    continue

                doc_lower = doc.lower()
                metadata = batch_docs['metadatas'][i]
                summary = metadata.get('summary', '').lower()

                # 计算匹配分数（确保不超过1.0）
                score = 0.0

                # 1. 标题匹配（高权重，最多0.3分）
                title_matches = 0
                for word in query_words:
                    if word in summary:
                        title_matches += 1
                # 标题匹配分数按匹配比例计算，最高0.3
                score += min(title_matches / len(query_words), 1.0) * 0.3

                # 2. 文档内容匹配（最多0.4分）
                content_matches = 0
                for word in query_words:
                    if word in doc_lower:
                        content_matches += 1

                if content_matches > 0:
                    # 基础分数：匹配词比例
                    coverage = content_matches / len(query_words)
                    score += coverage * 0.4

                # 3. 完全匹配加分（0.2分）
                if query_lower in doc_lower:
                    score += 0.2

                # 4. 标题中包含完整查询加分（0.1分）
                if query_lower in summary:
                    score += 0.1
                
                # 确保分数在0-1范围内
                score = min(score, 1.0)

                if score > 0.1:  # 降低阈值
                    matches.append({
                        'issue_key': metadata.get('issue_key'),
                        'summary': metadata.get('summary', ''),
                        'score': float(score),
                        'document': doc,
                        'metadata': metadata
                    })

        # 按分数排序
        matches.sort(key=lambda x: x['score'], reverse=True)
        return matches[:top_k]
    
    def get_issue_by_key(self, issue_key: str) -> Optional[Dict]:
        """根据工单编号获取向量记录"""
        try:
            result = self.issues_collection.get(
                ids=[f"issue_{issue_key}"],
                include=['metadatas', 'documents']
            )
            if result and result['ids']:
                return {
                    'issue_key': issue_key,
                    'document': result['documents'][0],
                    'metadata': result['metadatas'][0]
                }
        except Exception:
            pass
        return None
    
    # ==================== AI分析结果缓存 ====================
    
    def cache_analysis(self, issue_key: str, analysis: Dict, 
                       summary: str = "", ttl_days: int = 30):
        """
        缓存AI分析结果
        
        Args:
            analysis: {
                'recommended_team': str,
                'recommended_role': str,
                'functionality_impact': str,
                'solution_suggestion': str,
                'confidence': float,
                'similar_issues': List[str],
                'model_used': str
            }
        """
        # 写入门（C 治本）：拦截空工单/分析失败的占位 stub，不写进向量缓存。否则其占位 embedding_text
        # （"工单缺少标题和描述…"/"自动分析失败"）污染 bge 召回，且 model_used!=rule_engine 时可能被
        # 采纳门放过（63723 类的源头：空工单 stub 优先级高于文件富 entry，靠 816b430 才绕过）。
        # 保守判定：functionality_impact 占位 AND summary(标题) 占位/空，二者同时满足才拦——
        # 正常工单 summary=真实标题或 fi 有实质内容，不会被误伤。
        _fi_g = (analysis.get('functionality_impact', '') or '').strip()
        _sm_g = (summary or '').strip()
        _fi_bad = (_fi_g == '' or _fi_g.startswith('未知') or '工单信息完全为空' in _fi_g)
        _sm_bad = (_sm_g == '' or '工单缺少标题和描述' in _sm_g or '工单信息完全为空' in _sm_g)
        if _fi_bad and _sm_bad:
            print(f"[VectorStore] 写入门：拦截空工单占位 stub，不缓存 {issue_key} (fi='{_fi_g[:24]}')")
            return

        content_hash = hashlib.md5(summary.encode()).hexdigest()[:16] if summary else ""

        doc_id = f"analysis_{issue_key}"
        
        # 构建可序列化的元数据
        meta = {
            'issue_key': issue_key,
            'content_hash': content_hash,
            'recommended_team': analysis.get('recommended_team', ''),
            'recommended_role': analysis.get('recommended_role', ''),
            'functionality_impact': analysis.get('functionality_impact', '')[:500],
            'solution_suggestion': analysis.get('solution_suggestion', '')[:1000],
            'confidence': float(analysis.get('confidence', 0)),
            'similar_issues': json.dumps(analysis.get('similar_issues', [])[:5]),
            'model_used': analysis.get('model_used', 'unknown'),
            'is_reused': analysis.get('is_reused', False),
            'reused_from': analysis.get('reused_from', ''),
            'created_at': datetime.now().isoformat(),
            'expires_at': (datetime.now() + timedelta(days=ttl_days)).isoformat()
        }
        
        # 使用 summary（issue标题）作为向量优先，回退到 suggestion+impact，最后用 issue_key
        embedding_text = summary.strip() or f"{analysis.get('solution_suggestion', '')} {analysis.get('functionality_impact', '')}".strip() or issue_key
        
        # 如果没有embedding函数，跳过向量存储，但不报错
        if self.embedding_func is None:
            print(f"[VectorStore] 警告: 没有embedding函数，跳过向量存储，但分析结果仍通过内存返回")
            return

        # upsert 比 delete+add 更安全：避免 delete 成功但 add 失败导致数据丢失
        try:
            self.analysis_collection.upsert(
                ids=[doc_id],
                documents=[embedding_text],
                metadatas=[meta]
            )
        except Exception as e:
            print(f"[VectorStore] 缓存分析 upsert 失败: {e}")
            # 不抛出异常，让分析结果通过内存返回
    
    def get_cached_analysis(self, issue_key: str, 
                           max_age_days: int = 7) -> Optional[Dict]:
        """
        获取缓存的AI分析结果
        
        返回格式与原始analysis一致
        """
        try:
            result = self.analysis_collection.get(
                ids=[f"analysis_{issue_key}"],
                include=['metadatas']
            )
            
            if not result or not result['ids']:
                return None
            
            meta = result['metadatas'][0]
            
            # 检查过期（已过 expires_at 视为 stale 但仍可用，产品知识老化慢）
            expires_at = datetime.fromisoformat(meta.get('expires_at', '2000-01-01'))
            if datetime.now() > expires_at:
                return {'stale': True, **self._meta_to_analysis(meta)}

            # 检查年龄
            created_at = datetime.fromisoformat(meta.get('created_at', '2000-01-01'))
            if datetime.now() - created_at > timedelta(days=max_age_days):
                return {'stale': True, **self._meta_to_analysis(meta)}
            
            return self._meta_to_analysis(meta)
            
        except Exception as e:
            print(f"[Cache Error] {e}")
            return None
    
    def find_reusable_analysis(self, query: str, min_confidence: float = 0.8,
                               min_suggestion_similarity: float = 0.85) -> Optional[Dict]:
        """
        查找可复用的分析结果（基于处理建议的语义相似度）
        
        用于：新问题与历史问题的建议相似时，复用分析结果
        """
        if self.analysis_collection.count() == 0:
            return None
        
        results = self.analysis_collection.query(
            query_texts=[_apply_query_instruction(query)],
            n_results=3,
            where={"confidence": {"$gte": min_confidence}},  # 只考虑高可靠度结果
            include=['metadatas', 'distances']
        )
        
        if not results or not results['ids'][0]:
            return None
        
        # 检查最相似的结果
        best_distance = results['distances'][0][0]
        best_similarity = 1 - best_distance
        
        if best_similarity < min_suggestion_similarity:
            return None
        
        meta = results['metadatas'][0][0]
        analysis = self._meta_to_analysis(meta)
        analysis['is_reused'] = True
        analysis['reused_similarity'] = float(best_similarity)
        analysis['reused_from'] = meta.get('issue_key', '')
        
        return analysis
    
    def _meta_to_analysis(self, meta: Dict) -> Dict:
        """将元数据转换为analysis字典"""
        return {
            'recommended_team': meta.get('recommended_team', ''),
            'recommended_role': meta.get('recommended_role', ''),
            'functionality_impact': meta.get('functionality_impact', ''),
            'solution_suggestion': meta.get('solution_suggestion', ''),
            'confidence': float(meta.get('confidence', 0)),
            'similar_issues': json.loads(meta.get('similar_issues', '[]')),
            'model_used': meta.get('model_used', ''),
            'is_reused': meta.get('is_reused', 'False') == 'True',
            'reused_from': meta.get('reused_from', ''),
            'created_at': meta.get('created_at', '')
        }

    def invalidate_cache(self, issue_key: str) -> bool:
        """
        使指定工单的AI分析缓存失效

        通过设置过期时间为过去时间来实现
        """
        try:
            doc_id = f"analysis_{issue_key}"

            # 检查是否存在
            result = self.analysis_collection.get(ids=[doc_id])
            if not result or not result['ids']:
                return False

            # 更新元数据，标记为过期
            old_meta = result['metadatas'][0]
            old_meta['expires_at'] = '2000-01-01T00:00:00'  # 设为已过期
            old_meta['invalidated_at'] = datetime.now().isoformat()

            # Chroma需要先删除再添加来更新
            self.analysis_collection.delete(ids=[doc_id])
            self.analysis_collection.add(
                ids=[doc_id],
                documents=[old_meta.get('solution_suggestion', '')],
                metadatas=[old_meta]
            )

            print(f"[VectorStore] {issue_key} 缓存已失效")
            return True

        except Exception as e:
            print(f"[Invalidate Error] {e}")
            return False

    # ==================== 相似度图谱 ====================
    
    def record_similarity(self, issue_key: str, similar_key: str, 
                          similarity_score: float, can_reuse: bool = False):
        """记录两个工单间的相似度关系"""
        edge_id = f"sim_{issue_key}_{similar_key}"
        
        self.similarity_collection.add(
            ids=[edge_id],
            documents=[f"{issue_key} similar to {similar_key}"],
            metadatas=[{
                'source': issue_key,
                'target': similar_key,
                'similarity_score': float(similarity_score),
                'can_reuse': can_reuse,
                'created_at': datetime.now().isoformat()
            }]
        )
    
    def get_similar_neighbors(self, issue_key: str, 
                              min_score: float = 0.8) -> List[Dict]:
        """获取与指定工单相似的所有邻居"""
        try:
            results = self.similarity_collection.get(
                where={"source": issue_key, "similarity_score": {"$gte": min_score}},
                include=['metadatas']
            )
            
            neighbors = []
            for meta in results.get('metadatas', []):
                neighbors.append({
                    'issue_key': meta.get('target'),
                    'similarity': float(meta.get('similarity_score', 0)),
                    'can_reuse': meta.get('can_reuse', False)
                })
            
            return sorted(neighbors, key=lambda x: x['similarity'], reverse=True)
        except Exception:
            return []
    
    # ==================== 批量操作 ====================
    
    def batch_add_issues(self, issues: List[Dict]):
        """批量添加工单（用于初始化）"""
        ids = []
        documents = []
        metadatas = []
        
        for issue in issues:
            issue_key = issue.get('key')
            summary = issue.get('summary', '')
            description = issue.get('description', '')
            text = f"{summary} {description}"[:2000]
            
            content_hash = hashlib.md5(text.encode()).hexdigest()[:16]
            
            ids.append(f"issue_{issue_key}")
            documents.append(text)
            metadatas.append({
                'issue_key': issue_key,
                'summary': summary[:500],
                'content_hash': content_hash,
                'added_at': datetime.now().isoformat(),
                **{k: str(v)[:500] for k, v in issue.items() if k not in ['key', 'summary', 'description']}
            })
        
        # 分批添加（避免单次过多）
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            self.issues_collection.add(
                ids=ids[i:i+batch_size],
                documents=documents[i:i+batch_size],
                metadatas=metadatas[i:i+batch_size]
            )

    def batch_upsert_issues(self, issues: List[Dict]):
        """批量 upsert 工单（用于更新已有工单的内容）。

        与 batch_add_issues 的区别：upsert 对已存在 id 执行更新而非静默 no-op，
        确保描述变更的工单能被重新索引。
        """
        ids = []
        documents = []
        metadatas = []

        for issue in issues:
            issue_key = issue.get('key')
            summary = issue.get('summary', '')
            description = issue.get('description', '')
            text = f"{summary} {description}"[:2000]

            content_hash = hashlib.md5(text.encode()).hexdigest()[:16]

            ids.append(f"issue_{issue_key}")
            documents.append(text)
            metadatas.append({
                'issue_key': issue_key,
                'summary': summary[:500],
                'content_hash': content_hash,
                'added_at': datetime.now().isoformat(),
                **{k: str(v)[:500] for k, v in issue.items() if k not in ['key', 'summary', 'description']}
            })

        # 分批 upsert
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            self.issues_collection.upsert(
                ids=ids[i:i+batch_size],
                documents=documents[i:i+batch_size],
                metadatas=metadatas[i:i+batch_size]
            )

    def batch_add_generic_issues(self, issues) -> None:
        """Accept a list of GenericIssue objects and index them via batch_add_issues."""
        docs = [
            {
                "key": i.key,
                "summary": i.summary,
                "description": i.description,
                "source": i.source,
                **{k: v for k, v in i.extra.items() if isinstance(v, str)},
            }
            for i in issues
        ]
        self.batch_add_issues(docs)

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'issues_count': self.issues_collection.count(),
            'analysis_count': self.analysis_collection.count(),
            'similarity_edges': self.similarity_collection.count()
        }
    
    def cleanup_expired(self, dry_run: bool = True) -> int:
        """清理过期的分析缓存"""
        # 获取所有过期记录
        expired = self.analysis_collection.get(
            where={"expires_at": {"$lt": datetime.now().isoformat()}}
        )
        
        count = len(expired.get('ids', []))
        
        if not dry_run and count > 0:
            self.analysis_collection.delete(ids=expired['ids'])
        
        return count

    # ==================== 需求池 (Requirement Pool) 操作 ====================
    
    def upsert_requirement(self, req_id: str, title: str, description: str, metadata: Dict = None):
        """
        添加或更新需求池记录
        """
        doc_id = f"req_{req_id}"
        embedding_text = f"{title}\n{description}"[:2000]

        meta = {
            'req_id': req_id,
            'title': title[:500],
            'description': description[:2000], # store some of description in metadata
            'status': metadata.get('status', 'new') if metadata else 'new',
            'source_issues': json.dumps(metadata.get('source_issues', []) if metadata else []),
            'ai_analysis': json.dumps(metadata.get('ai_analysis', {}) if metadata else {}),
            'review_records': json.dumps(metadata.get('review_records', []) if metadata else {}),
            'entry_source': metadata.get('entry_source', '') if metadata else '',
            'requirement_fact_packet': json.dumps(metadata.get('requirement_fact_packet', {}) if metadata else {}),
            'created_at': metadata.get('created_at', datetime.now().isoformat()) if metadata else datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'feishu_notified': metadata.get('feishu_notified', False) if metadata else False,
            'topic_l1': metadata.get('topic_l1', '') if metadata else '',
            'topic_l2': metadata.get('topic_l2', '') if metadata else '',
        }

        if self.embedding_func is None:
            try:
                from kb_hybrid_index import LocalHashEmbeddingFunction
                from embedding_config import get_embedding_dim
                _local_ef = LocalHashEmbeddingFunction(dimensions=get_embedding_dim())
                embedding = _local_ef([embedding_text])[0]
                logger.info(
                    "[VectorStore] offline mode: req_pool using local-hash embedding for %s — "
                    "upgrade to bge-base-zh-v1.5 for real similarity",
                    doc_id,
                )
                self.req_pool_collection.upsert(
                    ids=[doc_id],
                    embeddings=[embedding],
                    metadatas=[meta],
                )
                return True
            except Exception as e:
                print(f"[VectorStore] Upsert requirement with local-hash embedding failed: {e}")
                return False
        else:
            # 使用embedding函数
            try:
                self.req_pool_collection.upsert(
                    ids=[doc_id],
                    documents=[embedding_text],
                    metadatas=[meta]
                )
                return True
            except Exception as e:
                print(f"[VectorStore] Upsert requirement failed: {e}")
                return False

    def get_requirement(self, req_id: str) -> Optional[Dict]:
        """获取指定的需求"""
        try:
            result = self.req_pool_collection.get(
                ids=[f"req_{req_id}"],
                include=['metadatas', 'documents']
            )
            if result and result['ids']:
                meta = result['metadatas'][0]
                return {
                    'req_id': meta.get('req_id'),
                    'title': meta.get('title', ''),
                    'description': meta.get('description', ''),
                    'status': meta.get('status', 'new'),
                    'source_issues': json.loads(meta.get('source_issues', '[]')),
                    'ai_analysis': json.loads(meta.get('ai_analysis', '{}')),
                    'review_records': json.loads(meta.get('review_records', '[]')),
                    'entry_source': meta.get('entry_source', ''),
                    'requirement_fact_packet': json.loads(meta.get('requirement_fact_packet', '{}')),
                    'created_at': meta.get('created_at'),
                    'updated_at': meta.get('updated_at'),
                    'feishu_notified': meta.get('feishu_notified', False),
                }
        except Exception as e:
             print(f"[VectorStore] Get requirement failed: {e}")
        return None

    def list_requirements(self, status: str = None, date_range: Dict = None) -> List[Dict]:
        """获取需求列表，支持状态筛选和日期范围筛选

        Args:
            status: 状态筛选
            date_range: 日期范围筛选 {'start': '2026-01-01', 'end': '2026-02-28'}
        """
        try:
            where_clause = {"status": status} if status else None

            result = self.req_pool_collection.get(
                where=where_clause,
                include=['metadatas']
            )

            reqs = []
            if result and result['metadatas']:
                for meta in result['metadatas']:
                    req = {
                        'req_id': meta.get('req_id'),
                        'title': meta.get('title', ''),
                        'description': meta.get('description', ''),
                        'status': meta.get('status', 'new'),
                        'source_issues': json.loads(meta.get('source_issues', '[]')),
                        'ai_analysis': json.loads(meta.get('ai_analysis', '{}')),
                        'review_records': json.loads(meta.get('review_records', '[]')),
                        'entry_source': meta.get('entry_source', ''),
                        'requirement_fact_packet': json.loads(meta.get('requirement_fact_packet', '{}')),
                        'created_at': meta.get('created_at'),
                        'updated_at': meta.get('updated_at'),
                        'feishu_notified': meta.get('feishu_notified', False),
                    }

                    # 内存中过滤日期范围
                    if date_range and req.get('created_at'):
                        req_date = req['created_at'][:10]  # 取YYYY-MM-DD部分
                        if date_range.get('start') and req_date < date_range['start']:
                            continue
                        if date_range.get('end') and req_date > date_range['end']:
                            continue

                    reqs.append(req)

            # 按创建时间倒序
            reqs.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            return reqs
        except Exception as e:
            print(f"[VectorStore] List requirements failed: {e}")
            return []

    def delete_requirement(self, req_id: str) -> bool:
        """删除指定的需求"""
        try:
            self.req_pool_collection.delete(ids=[f"req_{req_id}"])
            return True
        except Exception as e:
            print(f"[VectorStore] Delete requirement failed: {e}")
            return False

    def clear_requirements(self) -> bool:
        """清空所有需求（谨慎使用）"""
        try:
            # 获取所有需求ID
            result = self.req_pool_collection.get()
            if result and result['ids']:
                self.req_pool_collection.delete(ids=result['ids'])
            return True
        except Exception as e:
            print(f"[VectorStore] Clear requirements failed: {e}")
            return False

    def search_similar_requirements(self, query: str, top_k: int = 5) -> List[Dict]:
        """
        语义搜索相似的需求（用于查重）
        """
        if self.req_pool_collection.count() == 0:
            return []

        try:
            results = self.req_pool_collection.query(
                query_texts=[_apply_query_instruction(query)],
                n_results=min(top_k, self.req_pool_collection.count()),
                include=['metadatas', 'distances']
            )

            similar_reqs = []
            if results and results['ids'] and results['ids'][0]:
                for i in range(len(results['ids'][0])):
                    distance = results['distances'][0][i]
                    score = 1 - distance

                    meta = results['metadatas'][0][i]
                    similar_reqs.append({
                        'req_id': meta.get('req_id'),
                        'title': meta.get('title', ''),
                        'score': float(score),
                        'status': meta.get('status', 'new')
                    })

                similar_reqs.sort(key=lambda x: x['score'], reverse=True)
            return similar_reqs
        except Exception as e:
            print(f"[VectorStore] Search similar requirements failed: {e}")
            return []

    # ==================== 需求聚类操作 ====================

    def upsert_cluster(self, cluster_id: str, title: str, metadata: Dict) -> bool:
        """插入或更新一个需求聚类（theme）"""
        doc_id = f"cluster_{cluster_id}"
        embedding_text = title[:1000]
        meta = {k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
                for k, v in metadata.items()}
        meta["cluster_id"] = cluster_id
        meta["title"] = title[:500]
        meta.setdefault("status", "new")
        meta.setdefault("created_at", datetime.now().isoformat())
        meta["updated_at"] = datetime.now().isoformat()
        try:
            if self.embedding_func is None:
                from kb_hybrid_index import LocalHashEmbeddingFunction
                from embedding_config import get_embedding_dim
                _local_ef = LocalHashEmbeddingFunction(dimensions=get_embedding_dim())
                emb = _local_ef([embedding_text])[0]
                logger.info(
                    "[VectorStore] offline mode: req_clusters using local-hash embedding for %s",
                    doc_id,
                )
                self.req_clusters_collection.upsert(ids=[doc_id], embeddings=[emb], metadatas=[meta])
            else:
                self.req_clusters_collection.upsert(ids=[doc_id], documents=[embedding_text], metadatas=[meta])
            return True
        except Exception as e:
            print(f"[VectorStore] upsert_cluster failed: {e}")
            return False

    def get_cluster(self, cluster_id: str) -> Optional[Dict]:
        """按 cluster_id 获取聚类"""
        try:
            result = self.req_clusters_collection.get(ids=[f"cluster_{cluster_id}"], include=["metadatas"])
            if result and result["ids"]:
                return result["metadatas"][0]
        except Exception as e:
            print(f"[VectorStore] get_cluster failed: {e}")
        return None

    def list_clusters(self, status: str = None) -> List[Dict]:
        """列出所有聚类，可按 status 过滤"""
        try:
            where = {"status": status} if status else None
            result = self.req_clusters_collection.get(where=where, include=["metadatas"])
            clusters = result.get("metadatas", []) if result else []
            clusters.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return clusters
        except Exception as e:
            print(f"[VectorStore] list_clusters failed: {e}")
            return []

    def update_cluster_field(self, cluster_id: str, fields: Dict) -> bool:
        """局部更新聚类元数据字段"""
        cluster = self.get_cluster(cluster_id)
        if cluster is None:
            return False
        cluster.update({k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
                        for k, v in fields.items()})
        cluster["updated_at"] = datetime.now().isoformat()
        title = cluster.get("title", cluster_id)
        return self.upsert_cluster(cluster_id, title, cluster)

    def delete_cluster(self, cluster_id: str) -> bool:
        """删除一个聚类（硬取消清理用）"""
        try:
            self.req_clusters_collection.delete(ids=[f"cluster_{cluster_id}"])
            return True
        except Exception as e:
            print(f"[VectorStore] delete_cluster failed: {e}")
            return False

    def update_requirement_field(self, req_id: str, fields: Dict) -> bool:
        """局部更新需求元数据字段，fields 中值为 None 表示删除该字段"""
        doc_id = f"req_{req_id}"
        try:
            result = self.req_pool_collection.get(ids=[doc_id], include=["metadatas"])
            if not result or not result["ids"]:
                return False
            meta = result["metadatas"][0].copy()
            for k, v in fields.items():
                if v is None:
                    meta.pop(k, None)
                else:
                    meta[k] = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
            meta["updated_at"] = datetime.now().isoformat()
            self.req_pool_collection.update(ids=[doc_id], metadatas=[meta])
            return True
        except Exception as e:
            print(f"[VectorStore] update_requirement_field failed: {e}")
            return False

    # ==================== 查询缓存操作 ====================

    def save_query_cache(self, cache_key: str, query: str, content: str,
                         context_keys: List[str], ttl_hours: int = 24) -> bool:
        """
        保存查询结果到缓存

        Args:
            cache_key: 缓存唯一键
            query: 原始查询文本
            content: 缓存的内容
            context_keys: 相关的上下文键列表（如工单ID）
            ttl_hours: 缓存过期时间（小时），默认24小时

        Returns:
            bool: 保存是否成功
        """
        if not self.query_cache:
            return False

        query_embedding = self._get_query_embedding(query)

        try:
            from embedding_config import get_embedding_dim
            _zero = [0.0] * get_embedding_dim()
            self.query_cache.upsert(
                ids=[cache_key],
                documents=[content],
                embeddings=[query_embedding.tolist() if query_embedding is not None else _zero],
                metadatas=[{
                    "original_query": query,
                    "context_keys": json.dumps(context_keys),
                    "created_at": datetime.now().isoformat(),
                    "expire_at": (datetime.now() + timedelta(hours=ttl_hours)).isoformat(),
                    "hit_count": 0
                }]
            )
            return True
        except Exception as e:
            print(f"[VectorStore] Save query cache failed: {e}")
            return False

    def get_query_cache(self, cache_key: str) -> Optional[Dict]:
        """
        从缓存获取查询结果

        Args:
            cache_key: 缓存唯一键

        Returns:
            Dict: 缓存数据，包含content, original_query, context_keys, hit_count等
                  如果缓存不存在或已过期，返回None
        """
        if not self.query_cache:
            return None

        try:
            # 获取缓存数据（包含embedding）
            result = self.query_cache.get(
                ids=[cache_key],
                include=['metadatas', 'documents', 'embeddings']
            )

            if not result or not result['ids']:
                return None

            meta = result['metadatas'][0]

            # 检查过期时间
            expire_at = datetime.fromisoformat(meta.get('expire_at', '2000-01-01'))
            if datetime.now() > expire_at:
                return None  # 已过期

            # 更新命中次数
            current_hit_count = int(meta.get('hit_count', 0))
            meta['hit_count'] = current_hit_count + 1

            # 更新缓存记录（使用upsert），需要提供embedding
            from embedding_config import get_embedding_dim
            embedding = [0.0] * get_embedding_dim()  # 默认值（维度随 serving 模型，bge=768）
            embeddings_list = result.get('embeddings')
            if embeddings_list is not None and len(embeddings_list) > 0:
                first_embedding = embeddings_list[0]
                # 如果是numpy数组，转换为列表
                if isinstance(first_embedding, np.ndarray):
                    embedding = first_embedding.tolist()
                elif isinstance(first_embedding, list):
                    embedding = first_embedding

            self.query_cache.upsert(
                ids=[cache_key],
                documents=[result['documents'][0]],
                embeddings=[embedding],
                metadatas=[meta]
            )

            # 返回缓存数据
            return {
                'content': result['documents'][0],
                'original_query': meta.get('original_query', ''),
                'context_keys': json.loads(meta.get('context_keys', '[]')),
                'hit_count': meta['hit_count'],
                'created_at': meta.get('created_at', ''),
                'expire_at': meta.get('expire_at', '')
            }
        except Exception as e:
            print(f"[VectorStore] Get query cache failed: {e}")
            return None

    def _get_query_embedding(self, query: str) -> Optional[np.ndarray]:
        """
        获取查询文本的embedding向量

        Args:
            query: 查询文本

        Returns:
            np.ndarray: embedding向量，如果embedding函数不可用返回None
        """
        if not query or not self.embedding_func:
            return None

        try:
            embeddings = self.embedding_func([query])
            if embeddings and len(embeddings) > 0:
                return np.array(embeddings[0])
        except Exception as e:
            print(f"[VectorStore] Get query embedding failed: {e}")

        return None
