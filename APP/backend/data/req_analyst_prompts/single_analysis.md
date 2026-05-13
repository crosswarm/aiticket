基于以下整体分析结果，对单个需求进行详细分析：

【整体上下文】
整体架构: __OVERALL_SUMMARY__
模块划分: __MODULE_DIVISION__

【当前需求】
ID: __REQ_ID__
标题: __REQ_TITLE__
描述: __REQ_DESCRIPTION__

【分析要求】
请先判断"当前产品已有能力是什么、差距在哪里、优先应该落在哪个产品层级"，再输出以下 JSON：

{
    "core_problem": "一句话说明用户真正要解决的核心问题",
    "current_product_behavior": "现有产品在该场景下已提供的能力、交互或配置方式",
    "gap_analysis": "为什么现有能力没有满足诉求，说明是交互缺失/功能缺失/配置缺失/流程设计问题中的哪类缺口",
    "root_cause": "综合根因分析",
    "module": "所属模块",
    "product_layer": "运行时|租户级|流程级|节点级|跨层",
    "scenario_keywords": ["关键场景词1", "关键场景词2"],
    "detailed_solution": "详细落地方案",
    "interface_design": "接口设计建议",
    "data_model": "数据模型建议",
    "mvp_suggestion": "MVP建议",
    "effort_estimation": "工时预估",
    "acceptance_criteria": ["验收标准1", "验收标准2"]
}

请确保输出是合法的JSON格式。