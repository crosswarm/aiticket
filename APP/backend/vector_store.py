"""
Chroma Vector Database Manager
管理工单向量存储、AI分析结果缓存、相似度关系
"""

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
import json
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import asdict
import numpy as np
import os


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

    # 方案1: 本地多语言模型（推荐，无需API）
    try:
        ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"  # 多语言，384维
        )
        print("[VectorStore] 使用本地嵌入模型: paraphrase-multilingual-MiniLM-L12-v2")
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
        self.req_clusters_collection = self._get_or_create_collection("req_clusters")

        # 初始化查询缓存集合
        self._init_query_cache_collection()

        print(f"[VectorStore] 初始化完成")
        print(f"  - 工单集合: {self._safe_collection_count(self.issues_collection)} 条")
        print(f"  - 分析缓存: {self._safe_collection_count(self.analysis_collection)} 条")
        print(f"  - 相似度图: {self._safe_collection_count(self.similarity_collection)} 条")
        print(f"  - 需求聚类: {self._safe_collection_count(self.req_clusters_collection)} 条")
        if self.query_cache:
            print(f"  - 查询缓存: {self._safe_collection_count(self.query_cache)} 条")
    
    def _get_or_create_collection(self, name: str):
        """获取或创建集合"""
        try:
            return self.client.get_collection(
                name=name,
                embedding_function=self.embedding_func
            )
        except Exception:
            return self.client.create_collection(
                name=name,
                embedding_function=self.embedding_func,
                metadata={"hnsw:space": "cosine"}  # 使用余弦相似度
            )

    def _safe_collection_count(self, collection) -> int:
        try:
            return int(collection.count())
        except Exception as e:
            print(f"[VectorStore] 集合计数失败，按 0 处理: {e}")
            return 0

    def _init_query_cache_collection(self):
        """初始化查询缓存集合"""
        try:
            self.query_cache = self.client.get_or_create_collection(
                name="query_cache",
                embedding_function=self.embedding_func,
                metadata={"description": "查询结果缓存，TTL 24小时"}
            )
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
            issue_key: 工单编号 MYPROJECT-12345
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
                              min_score: float = 0.7) -> List[Dict]:
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
                query_texts=[query],
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
        return similar_issues[:top_k]

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
        
        # 使用 suggestion + impact 作为向量（用于相似建议复用）
        embedding_text = f"{analysis.get('solution_suggestion', '')} {analysis.get('functionality_impact', '')}"
        
        # 删除旧记录
        try:
            self.analysis_collection.delete(ids=[doc_id])
        except Exception:
            pass

        # 如果没有embedding函数，跳过向量存储，但不报错
        if self.embedding_func is None:
            print(f"[VectorStore] 警告: 没有embedding函数，跳过向量存储，但分析结果仍通过内存返回")
            # 不返回，继续执行回调
            return

        try:
            self.analysis_collection.add(
                ids=[doc_id],
                documents=[embedding_text],
                metadatas=[meta]
            )
        except Exception as e:
            print(f"[VectorStore] 缓存分析失败: {e}")
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
            
            # 检查过期
            expires_at = datetime.fromisoformat(meta.get('expires_at', '2000-01-01'))
            if datetime.now() > expires_at:
                return None  # 已过期
            
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
            query_texts=[query],
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
                import numpy as np, hashlib
                seed = int.from_bytes(hashlib.sha256(embedding_text.encode()).digest()[:4], 'big')
                np.random.seed(seed)
                emb = (np.random.randn(384).astype(np.float32))
                emb /= np.linalg.norm(emb)
                self.req_clusters_collection.upsert(ids=[doc_id], embeddings=[emb.tolist()], metadatas=[meta])
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
            self.query_cache.upsert(
                ids=[cache_key],
                documents=[content],
                embeddings=[query_embedding.tolist() if query_embedding is not None else [0.0] * 384],
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
            embedding = [0.0] * 384  # 默认值
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
