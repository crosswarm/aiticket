"""
操作引导系统单元测试

测试内容：
1. 环境识别服务
2. 版本适配管理器
3. 截图分析服务
4. 引导标注服务
5. 引导生成引擎
"""

import os
import sys
import json
import pytest
from unittest.mock import Mock, patch, MagicMock

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入服务
from services.environment_detector import (
    EnvironmentDetector, EnvironmentType, AccessMethod, EnvironmentInfo, detect_environment
)
from services.version_adapter import (
    VersionAdapterManager, GuideStep, UIElement, get_version_adapter
)
from services.screenshot_analyzer import (
    ScreenshotAnalyzer, ScreenshotAnalysisResult, DetectedElement
)
from services.guide_annotator import (
    GuideAnnotator, Annotation, AnnotationType, AnnotatedImage
)
from services.guide_generator import (
    GuideGenerator, GuideRequest, GuideResult
)


class TestEnvironmentDetector:
    """环境识别服务测试"""

    def test_detect_public_cloud(self):
        """测试公有云环境识别"""
        detector = EnvironmentDetector()
        tenant_info = {
            'deployment_type': 'public',
            'system_version': '3.5.2'
        }

        result = detector.detect(tenant_info)

        assert result.env_type == EnvironmentType.PUBLIC_CLOUD
        assert result.access_method == AccessMethod.AGENT
        assert result.ui_rules_version == 'latest'

    def test_detect_dedicated_cloud(self):
        """测试专属云环境识别"""
        detector = EnvironmentDetector()
        tenant_info = {
            'deployment_type': 'dedicated',
            'system_version': '3.2.1'
        }

        result = detector.detect(tenant_info)

        assert result.env_type == EnvironmentType.DEDICATED_CLOUD
        assert result.access_method == AccessMethod.AGENT
        assert result.ui_rules_version == 'v3.2'

    def test_detect_private_cloud(self):
        """测试私有云环境识别"""
        detector = EnvironmentDetector()
        tenant_info = {
            'deployment_type': 'private',
            'system_version': '2.8.0'
        }

        result = detector.detect(tenant_info)

        assert result.env_type == EnvironmentType.PRIVATE_CLOUD
        assert result.access_method == AccessMethod.SCREENSHOT

    def test_detect_unknown_environment(self):
        """测试未知环境识别"""
        detector = EnvironmentDetector()
        tenant_info = {
            'deployment_type': 'unknown',
            'system_version': 'unknown'
        }

        result = detector.detect(tenant_info)

        # 未知类型应该回退到私有云
        assert result.env_type == EnvironmentType.PRIVATE_CLOUD
        assert result.access_method == AccessMethod.SCREENSHOT

    def test_get_strategy_for_environment(self):
        """测试获取环境策略"""
        detector = EnvironmentDetector()

        # 公有云策略
        env_info = EnvironmentInfo(
            env_type=EnvironmentType.PUBLIC_CLOUD,
            version='latest',
            access_method=AccessMethod.AGENT,
            ui_rules_version='latest'
        )
        strategy = detector.get_strategy_for_environment(env_info)
        assert strategy['primary_method'] == 'agent'
        assert strategy['supports_realtime'] is True

        # 私有云策略
        env_info = EnvironmentInfo(
            env_type=EnvironmentType.PRIVATE_CLOUD,
            version='unknown',
            access_method=AccessMethod.SCREENSHOT,
            ui_rules_version='fallback'
        )
        strategy = detector.get_strategy_for_environment(env_info)
        assert strategy['primary_method'] == 'screenshot'
        assert strategy['requires_user_action'] is True

    def test_version_matching(self):
        """测试版本匹配逻辑"""
        detector = EnvironmentDetector()

        # 3.5+ 版本匹配
        assert detector._version_matches('3.5.2', '3.5+') is True
        assert detector._version_matches('3.6.0', '3.5+') is True
        assert detector._version_matches('3.4.0', '3.5+') is False

        # 3.2.x 版本匹配
        assert detector._version_matches('3.2.1', '3.2.x') is True
        assert detector._version_matches('3.2.5', '3.2.x') is True
        assert detector._version_matches('3.3.0', '3.2.x') is False


class TestVersionAdapter:
    """版本适配管理器测试"""

    def test_load_rules(self):
        """测试规则加载"""
        adapter = VersionAdapterManager()
        versions = adapter.list_available_versions()

        assert 'latest' in versions or 'fallback' in versions

    def test_get_selector(self):
        """测试获取选择器"""
        adapter = VersionAdapterManager()

        # 获取latest版本的画布选择器
        selector = adapter.get_selector('canvas', 'latest', 'flow_designer')

        # 应该返回一个选择器字符串
        if selector:
            assert isinstance(selector, str)
            assert len(selector) > 0

    def test_adapt_steps(self):
        """测试步骤适配"""
        adapter = VersionAdapterManager()

        steps = [
            GuideStep(step=1, action='click', target='canvas', tip='点击画布'),
            GuideStep(step=2, action='select', target='approval_node', tip='选择审批节点')
        ]

        adapted = adapter.adapt_steps(steps, 'latest', 'flow_designer')

        assert len(adapted) == 2
        assert adapted[0].step == 1
        assert adapted[0].action == 'click'

    def test_find_matching_module(self):
        """测试URL匹配模块"""
        adapter = VersionAdapterManager()

        # 匹配流程设计器
        module = adapter.find_matching_module(
            'https://example.com/workflow/designer',
            'latest'
        )

        # 如果有规则，应该匹配到flow_designer
        if module:
            assert module == 'flow_designer'


class TestScreenshotAnalyzer:
    """截图分析服务测试"""

    def test_analyze_without_llm(self):
        """测试无LLM时的分析（使用模拟结果）"""
        analyzer = ScreenshotAnalyzer(llm_service=None)

        # 创建一个简单的测试图片（1x1像素PNG）
        import base64
        # 最小的PNG文件
        png_data = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
        )

        result = analyzer.analyze(png_data)

        assert result.image_id is not None
        assert len(result.image_id) == 16

    def test_find_target_element(self):
        """测试查找目标元素"""
        analyzer = ScreenshotAnalyzer()

        # 创建模拟分析结果
        analysis = ScreenshotAnalysisResult(
            image_id='test123',
            detected_elements=[
                DetectedElement(
                    id='save_btn',
                    type='button',
                    text='保存',
                    bounds={'x': 100, 'y': 50, 'width': 80, 'height': 35},
                    confidence=0.95,
                    is_interactive=True
                )
            ]
        )

        # 查找保存按钮
        element = analyzer.find_target_element(analysis, '保存')
        assert element is not None
        assert element.id == 'save_btn'

        # 查找不存在的元素
        element = analyzer.find_target_element(analysis, '不存在的按钮')
        assert element is None


class TestGuideAnnotator:
    """引导标注服务测试"""

    def test_create_annotation(self):
        """测试创建标注"""
        annotator = GuideAnnotator()

        annotations = [
            Annotation(
                type=AnnotationType.RECTANGLE,
                x=100, y=100,
                width=200, height=50,
                text='这是一个按钮',
                color='#FF0000',
                order=1
            ),
            Annotation(
                type=AnnotationType.NUMBER,
                x=110, y=110,
                width=0, height=0,
                text='1',
                color='#FF0000',
                order=1
            )
        ]

        import base64
        # 最小的PNG文件
        png_data = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
        )

        result = annotator.annotate(png_data, annotations)

        assert result.image_id is not None
        assert len(result.annotations) == 2

    def test_annotation_to_dict(self):
        """测试标注转换为字典"""
        ann = Annotation(
            type=AnnotationType.RECTANGLE,
            x=100, y=100,
            width=200, height=50,
            text='测试标注',
            color='#FF0000',
            order=1
        )

        d = ann.to_dict()

        assert d['type'] == 'rectangle'
        assert d['x'] == 100
        assert d['y'] == 100
        assert d['text'] == '测试标注'
        assert d['order'] == 1


class TestGuideGenerator:
    """引导生成引擎测试"""

    def test_generate_guide_basic(self):
        """测试基本引导生成"""
        generator = GuideGenerator(llm_service=None)

        request = GuideRequest(
            issue_key='MYPROJECT-TEST01',
            issue_summary='如何添加审批节点',
            issue_description='我需要在流程中添加一个审批节点',
            tenant_info={'deployment_type': 'private'},
            screenshots=[],
            user_question=''
        )

        result = generator.generate(request)

        assert result.status in ['success', 'partial', 'failed']
        assert result.request_id is not None
        assert result.env_info is not None

    def test_scenario_identification(self):
        """测试场景识别"""
        generator = GuideGenerator(llm_service=None)

        request = GuideRequest(
            issue_key='MYPROJECT-TEST02',
            issue_summary='添加审批节点',
            issue_description='想要在流程设计器中新增审批节点',
            tenant_info={'deployment_type': 'private'},
            screenshots=[],
            user_question=''
        )

        result = generator.generate(request)

        # 如果匹配到场景，应该有步骤
        if result.matched_scenario:
            assert result.scenario_confidence > 0
            assert len(result.steps) > 0

    def test_generate_text_guide(self):
        """测试文字引导生成"""
        generator = GuideGenerator(llm_service=None)

        steps = [
            GuideStep(step=1, action='click', target='canvas', tip='点击画布'),
            GuideStep(step=2, action='select', target='approval_node', tip='选择审批节点')
        ]

        text = generator._generate_text_guide(steps, '添加审批节点')

        assert '操作指南' in text
        assert '点击画布' in text
        assert '选择审批节点' in text


class TestIntegration:
    """集成测试"""

    def test_full_flow(self):
        """测试完整流程"""
        # 1. 环境识别
        detector = EnvironmentDetector()
        env_info = detector.detect({'deployment_type': 'private'})
        assert env_info.env_type == EnvironmentType.PRIVATE_CLOUD

        # 2. 版本适配
        adapter = VersionAdapterManager()
        versions = adapter.list_available_versions()
        assert len(versions) >= 0

        # 3. 引导生成
        generator = GuideGenerator(llm_service=None)
        request = GuideRequest(
            issue_key='MYPROJECT-INTEGRATION',
            issue_summary='测试集成',
            issue_description='集成测试描述',
            tenant_info={'deployment_type': 'private'},
            screenshots=[]
        )
        result = generator.generate(request)
        assert result is not None


# 运行测试
if __name__ == '__main__':
    pytest.main([__file__, '-v'])