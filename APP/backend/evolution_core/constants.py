"""
Darwin 进化框架共享常量
v5 实战教训内嵌：MiniMax 推理模型参数、并发限制
"""

# MiniMax-M2.7 等推理模型：<think> block 消耗 1500+ tokens，必须留足空间
MAX_TOKENS_REASONING = 4096
# 非推理模型（GLM-5 等）
MAX_TOKENS_STANDARD = 2048
# 推理模型单批上限（小批次更稳定）
BATCH_SIZE_REASONING = 10
# 防 minimax API 529 overloaded
MAX_CONCURRENT_CLASSIFY_SCRIPTS = 2

# evolution 输出目录（相对 project root，用 Path.cwd() 拼接）
EVOLUTION_DIR = "conclusion/_local/evolution"

# BAD_VALUES：占位词精确集合
# 警告：禁止包含 '-'（末级主题格式是 X-Y，子串匹配会误判所有合法主题）
BAD_VALUES = frozenset(["未知", "未分类", "信息不足", "待确认", "N/A", ""])
