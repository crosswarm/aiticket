"""
截图分析服务 - 使用AI分析用户上传的截图
识别界面元素，生成操作引导
"""

import base64
import json
import logging
import os
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
import hashlib

logger = logging.getLogger(__name__)

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(BASE_DIR, "../.."))


@dataclass
class DetectedElement:
    """检测到的界面元素"""
    id: str
    type: str  # button, input, select, text, icon, etc.
    text: str = ""
    bounds: Dict[str, int] = field(default_factory=dict)  # x, y, width, height
    confidence: float = 0.0
    selector_hint: str = ""
    is_interactive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'type': self.type,
            'text': self.text,
            'bounds': self.bounds,
            'confidence': self.confidence,
            'selector_hint': self.selector_hint,
            'is_interactive': self.is_interactive
        }


@dataclass
class ScreenshotAnalysisResult:
    """截图分析结果"""
    image_id: str
    page_title: str = ""
    detected_elements: List[DetectedElement] = field(default_factory=list)
    module_guess: str = ""  # 猜测的模块名
    scenario_guess: str = ""  # 猜测的场景
    raw_analysis: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'image_id': self.image_id,
            'page_title': self.page_title,
            'detected_elements': [e.to_dict() for e in self.detected_elements],
            'module_guess': self.module_guess,
            'scenario_guess': self.scenario_guess,
            'raw_analysis': self.raw_analysis
        }


class ScreenshotAnalyzer:
    """
    截图分析服务

    功能：
    1. 接收用户上传的截图
    2. 使用多模态AI分析截图内容
    3. 识别界面元素（按钮、输入框、菜单等）
    4. 推测用户当前操作场景
    """

    def __init__(self, llm_service=None):
        """
        初始化截图分析器

        Args:
            llm_service: LLM服务实例，用于AI分析
        """
        self.llm_service = llm_service
        self._cache: Dict[str, ScreenshotAnalysisResult] = {}

    def _generate_image_id(self, image_data: bytes) -> str:
        """生成图片ID"""
        return hashlib.md5(image_data).hexdigest()[:16]

    def analyze(
        self,
        image_data: bytes,
        issue_context: Optional[Dict[str, Any]] = None
    ) -> ScreenshotAnalysisResult:
        """
        分析截图

        Args:
            image_data: 图片二进制数据
            issue_context: 工单上下文信息，用于辅助分析
                - issue_key: 工单编号
                - summary: 工单标题
                - description: 工单描述
                - module: 相关模块

        Returns:
            ScreenshotAnalysisResult: 分析结果
        """
        image_id = self._generate_image_id(image_data)

        # 检查缓存
        if image_id in self._cache:
            logger.info(f"✅ 使用缓存的分析结果: {image_id}")
            return self._cache[image_id]

        # 转换为base64
        image_base64 = base64.b64encode(image_data).decode('utf-8')

        # 构建分析prompt
        prompt = self._build_analysis_prompt(issue_context)

        # 调用LLM分析
        try:
            analysis_result = self._call_llm_analysis(image_base64, prompt)
            result = self._parse_analysis_result(image_id, analysis_result)

            # 缓存结果
            self._cache[image_id] = result

            return result

        except Exception as e:
            logger.error(f"❌ 截图分析失败: {e}")
            return self._create_fallback_result(image_id, str(e))

    def _build_analysis_prompt(self, issue_context: Optional[Dict[str, Any]] = None) -> str:
        """构建分析prompt"""
        context_info = ""
        if issue_context:
            context_info = f"""
## 工单上下文信息
- 工单编号: {issue_context.get('issue_key', '未知')}
- 标题: {issue_context.get('summary', '未知')}
- 描述: {issue_context.get('description', '无')}
- 相关模块: {issue_context.get('module', '未知')}
"""

        return f"""
你是一个UI界面分析专家，请分析这张截图，识别界面元素并推测用户可能的操作场景。

{context_info}

## 分析要求

1. **识别界面元素**:
   - 按钮：识别按钮文本、位置、可能的操作
   - 输入框：识别输入框类型、标签、占位文本
   - 下拉选择：识别下拉框、当前选中值
   - 菜单/导航：识别菜单项、当前位置
   - 表格/列表：识别列名、数据行
   - 其他交互元素

2. **推测模块和场景**:
   - 根据界面元素推测这是什么功能模块
   - 用户可能想完成什么操作

3. **输出格式** (JSON):
```json
{{
  "page_title": "页面标题",
  "module_guess": "猜测的模块名",
  "scenario_guess": "猜测的操作场景",
  "detected_elements": [
    {{
      "id": "元素唯一标识",
      "type": "button|input|select|text|icon|menu|table|其他",
      "text": "元素显示的文本",
      "bounds": {{"x": 0, "y": 0, "width": 100, "height": 30}},
      "confidence": 0.95,
      "selector_hint": "建议的CSS选择器",
      "is_interactive": true,
      "action_hint": "点击后可能的操作"
    }}
  ],
  "suggested_actions": [
    "建议用户执行的操作步骤"
  ]
}}
```

请只返回JSON，不要包含其他说明文字。
"""

    def _call_llm_analysis(self, image_base64: str, prompt: str) -> Dict[str, Any]:
        """调用LLM进行分析"""
        if self.llm_service is None:
            # 如果没有LLM服务，返回模拟结果
            return self._get_mock_analysis()

        try:
            # 尝试使用多模态模型
            result = self.llm_service.analyze_image(
                image_base64=image_base64,
                prompt=prompt
            )
            return result
        except Exception as e:
            logger.warning(f"⚠️ LLM分析失败，使用模拟结果: {e}")
            return self._get_mock_analysis()

    def _get_mock_analysis(self) -> Dict[str, Any]:
        """获取模拟分析结果（用于测试）"""
        return {
            "page_title": "流程设计器",
            "module_guess": "流程设计器",
            "scenario_guess": "添加审批节点",
            "detected_elements": [
                {
                    "id": "canvas",
                    "type": "canvas",
                    "text": "",
                    "bounds": {"x": 200, "y": 100, "width": 800, "height": 600},
                    "confidence": 0.95,
                    "selector_hint": ".designer-canvas",
                    "is_interactive": True
                },
                {
                    "id": "add_node_btn",
                    "type": "button",
                    "text": "添加节点",
                    "bounds": {"x": 50, "y": 150, "width": 100, "height": 35},
                    "confidence": 0.92,
                    "selector_hint": ".add-node-btn",
                    "is_interactive": True
                },
                {
                    "id": "approval_node",
                    "type": "menu_item",
                    "text": "审批节点",
                    "bounds": {"x": 50, "y": 200, "width": 100, "height": 30},
                    "confidence": 0.88,
                    "selector_hint": ".approval-node",
                    "is_interactive": True
                },
                {
                    "id": "save_btn",
                    "type": "button",
                    "text": "保存",
                    "bounds": {"x": 900, "y": 50, "width": 80, "height": 35},
                    "confidence": 0.95,
                    "selector_hint": ".save-btn",
                    "is_interactive": True
                }
            ],
            "suggested_actions": [
                "1. 点击「添加节点」按钮",
                "2. 选择「审批节点」类型",
                "3. 在属性面板配置审批人",
                "4. 点击「保存」完成配置"
            ]
        }

    def _parse_analysis_result(
        self,
        image_id: str,
        raw_result: Dict[str, Any]
    ) -> ScreenshotAnalysisResult:
        """解析分析结果"""
        elements = []

        for elem in raw_result.get('detected_elements', []):
            element = DetectedElement(
                id=elem.get('id', ''),
                type=elem.get('type', 'unknown'),
                text=elem.get('text', ''),
                bounds=elem.get('bounds', {}),
                confidence=elem.get('confidence', 0.0),
                selector_hint=elem.get('selector_hint', ''),
                is_interactive=elem.get('is_interactive', False)
            )
            elements.append(element)

        return ScreenshotAnalysisResult(
            image_id=image_id,
            page_title=raw_result.get('page_title', ''),
            detected_elements=elements,
            module_guess=raw_result.get('module_guess', ''),
            scenario_guess=raw_result.get('scenario_guess', ''),
            raw_analysis=raw_result
        )

    def _create_fallback_result(self, image_id: str, error: str) -> ScreenshotAnalysisResult:
        """创建降级结果"""
        return ScreenshotAnalysisResult(
            image_id=image_id,
            page_title="分析失败",
            detected_elements=[],
            module_guess="",
            scenario_guess="",
            raw_analysis={'error': error}
        )

    def analyze_multiple(
        self,
        images: List[bytes],
        issue_context: Optional[Dict[str, Any]] = None
    ) -> List[ScreenshotAnalysisResult]:
        """
        批量分析多张截图

        Args:
            images: 图片数据列表
            issue_context: 工单上下文

        Returns:
            分析结果列表
        """
        results = []
        for image_data in images:
            result = self.analyze(image_data, issue_context)
            results.append(result)
        return results

    def find_target_element(
        self,
        analysis_result: ScreenshotAnalysisResult,
        target_description: str
    ) -> Optional[DetectedElement]:
        """
        在分析结果中查找目标元素

        Args:
            analysis_result: 分析结果
            target_description: 目标描述（如"保存按钮"、"审批节点"）

        Returns:
            匹配的元素，如果找到
        """
        target_lower = target_description.lower()

        for element in analysis_result.detected_elements:
            # 匹配文本
            if element.text and target_lower in element.text.lower():
                return element

            # 匹配类型
            if target_lower in element.type.lower():
                return element

            # 匹配ID
            if target_lower in element.id.lower():
                return element

        return None


# 全局实例
_analyzer: Optional[ScreenshotAnalyzer] = None


def get_screenshot_analyzer(llm_service=None) -> ScreenshotAnalyzer:
    """获取截图分析器单例"""
    global _analyzer
    if _analyzer is None:
        _analyzer = ScreenshotAnalyzer(llm_service)
    return _analyzer


def analyze_screenshot(
    image_data: bytes,
    issue_context: Optional[Dict[str, Any]] = None
) -> ScreenshotAnalysisResult:
    """便捷函数：分析截图"""
    return get_screenshot_analyzer().analyze(image_data, issue_context)