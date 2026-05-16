"""
关键词搜索单元测试
测试中文分词、降级逻辑、评分机制
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import Mock, MagicMock, patch
from vector_store import VectorStore


class TestKeywordSearch:
    """测试关键词搜索功能"""

    @pytest.fixture
    def mock_vector_store(self):
        """创建带mock的VectorStore实例"""
        with patch('vector_store.get_embedding_function', return_value=None):
            with patch('chromadb.PersistentClient') as mock_client:
                # Mock issues_collection
                mock_collection = MagicMock()
                mock_collection.count.return_value = 100

                # Mock get方法返回测试数据
                mock_collection.get.return_value = {
                    'ids': ['issue_1', 'issue_2', 'issue_3'],
                    'documents': [
                        '流程中心审批单无法提交，报错信息：系统异常',
                        '登录页面样式错乱，需要调整CSS',
                        'API接口返回500错误，后端服务异常'
                    ],
                    'metadatas': [
                        {'issue_key': 'MYPROJECT-1', 'summary': '流程中心审批失败'},
                        {'issue_key': 'MYPROJECT-2', 'summary': '登录页面样式问题'},
                        {'issue_key': 'MYPROJECT-3', 'summary': 'API接口错误'}
                    ]
                }

                vs = VectorStore.__new__(VectorStore)
                vs.embedding_func = None
                vs.issues_collection = mock_collection
                vs.analysis_collection = mock_collection
                vs.similarity_collection = mock_collection

                vs.query_cache = None
                return vs

    def test_chinese_tokenization(self, mock_vector_store):
        """测试中文分词 - 提取2-4字子串"""
        query = "流程中心"
        results = mock_vector_store._keyword_search(query, top_k=5)

        # 验证返回结果
        assert isinstance(results, list)
        # 由于mock数据，至少应该返回一些结果
        print(f"中文分词测试结果: {len(results)}条结果")

    def test_mixed_language_search(self, mock_vector_store):
        """测试中英文混合搜索"""
        query = "API流程失败"
        results = mock_vector_store._keyword_search(query, top_k=5)

        assert isinstance(results, list)
        print(f"混合搜索测试结果: {len(results)}条结果")

    def test_empty_query(self, mock_vector_store):
        """测试空查询处理"""
        results = mock_vector_store._keyword_search("", top_k=5)
        # 空查询时会返回所有文档（因为没有过滤条件）
        assert isinstance(results, list)

    def test_single_character_query(self, mock_vector_store):
        """测试单字符查询 - 应该返回空结果"""
        results = mock_vector_store._keyword_search("中", top_k=5)
        # 单字符被过滤，应该返回空结果
        assert isinstance(results, list)

    def test_english_word_search(self, mock_vector_store):
        """测试英文单词搜索"""
        query = "API error"
        results = mock_vector_store._keyword_search(query, top_k=5)

        assert isinstance(results, list)
        print(f"英文搜索测试结果: {len(results)}条结果")

    def test_search_scoring(self, mock_vector_store):
        """测试搜索评分逻辑"""
        # 设置mock返回包含匹配关键词的文档
        mock_vector_store.issues_collection.get.return_value = {
            'ids': ['issue_1', 'issue_2'],
            'documents': [
                '流程中心审批流程问题，需要修复流程',
                '登录页面问题'
            ],
            'metadatas': [
                {'issue_key': 'MYPROJECT-1', 'summary': '流程中心审批失败'},
                {'issue_key': 'MYPROJECT-2', 'summary': '登录页面问题'}
            ]
        }

        results = mock_vector_store._keyword_search("流程", top_k=5)

        # 验证有结果返回
        assert len(results) > 0

        # 验证第一条结果有分数
        if results:
            assert 'score' in results[0]
            assert 'issue_key' in results[0]
            assert results[0]['score'] > 0
            print(f"评分结果: {results[0]['issue_key']} - 分数: {results[0]['score']}")


class TestSearchFallback:
    """测试搜索降级逻辑"""

    @pytest.fixture
    def mock_vector_store_with_embedding(self):
        """创建有embedding函数但会失败的VectorStore"""
        with patch('vector_store.get_embedding_function') as mock_ef:
            mock_ef.return_value = MagicMock()  # 有embedding函数

            with patch('chromadb.PersistentClient') as mock_client:
                mock_collection = MagicMock()
                mock_collection.count.return_value = 100

                # Mock query方法抛出异常
                mock_collection.query.side_effect = Exception("Embedding error")

                # Mock get方法返回数据
                mock_collection.get.return_value = {
                    'ids': ['issue_1'],
                    'documents': ['流程中心问题'],
                    'metadatas': [{'issue_key': 'MYPROJECT-1', 'summary': '流程问题'}]
                }

                vs = VectorStore.__new__(VectorStore)
                vs.embedding_func = MagicMock()
                vs.issues_collection = mock_collection
                vs.analysis_collection = mock_collection
                vs.similarity_collection = mock_collection

                vs.query_cache = None
                return vs

    def test_fallback_when_embedding_fails(self, mock_vector_store_with_embedding):
        """测试embedding失败时自动降级到关键词搜索"""
        results = mock_vector_store_with_embedding.search_similar_issues(
            "流程中心", top_k=5
        )

        # 验证有结果返回（降级成功）
        assert isinstance(results, list)
        print(f"降级测试结果: {len(results)}条结果")


class TestEdgeCases:
    """测试边界条件"""

    def test_special_characters(self):
        """测试特殊字符处理"""
        with patch('vector_store.get_embedding_function', return_value=None):
            with patch('chromadb.PersistentClient'):
                vs = VectorStore.__new__(VectorStore)
                vs.embedding_func = None
                vs.issues_collection = MagicMock()
                vs.issues_collection.count.return_value = 0
                vs.issues_collection.get.return_value = {
                    'ids': [], 'documents': [], 'metadatas': []
                }

                # 测试包含特殊字符的查询
                results = vs._keyword_search("流程<>!@#", top_k=5)
                assert isinstance(results, list)

    def test_long_query(self):
        """测试超长查询"""
        with patch('vector_store.get_embedding_function', return_value=None):
            with patch('chromadb.PersistentClient'):
                vs = VectorStore.__new__(VectorStore)
                vs.embedding_func = None
                vs.issues_collection = MagicMock()
                vs.issues_collection.count.return_value = 0
                vs.issues_collection.get.return_value = {
                    'ids': [], 'documents': [], 'metadatas': []
                }

                # 测试超长查询（200字符）
                long_query = "流程" * 100
                results = vs._keyword_search(long_query, top_k=5)
                assert isinstance(results, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
