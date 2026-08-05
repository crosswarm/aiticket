"""llm_feature_routing.json 的默认值与初始化。

## 为什么存在

这个文件此前【被 git 跟踪，却在运行时被设置页改写】，是同类配置里的异类
（同目录的 llm_config.json、config/deployment.yaml 早就是"忽略 + 不跟踪"）。

后果实测：172 线上是全 deepseek，仓库版是 minimax/zhipu，长期分叉；
更危险的是——只要有人改一次仓库里的这个文件，172 下次 git pull 就会
把线上路由悄悄换成 minimax，而 minimax 在那台机器上未必配了 key。
另外它每次被改写都会让工作区变脏，挡住 `git pull --ff-only`
（本会话已为它做过两轮"备份→还原"）。

## 做法

默认值收进代码（本文件），运行时文件不入库、缺失时自动生成。
这样 8 个直接读该文件的消费方（identity_schema / reply_supervisor /
board_service_chroma / reply_diff_analyzer / pattern_learning_agent 等）
全部零改动。

只依赖标准库。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ROUTING_FILENAME = "llm_feature_routing.json"
ROUTING_PATH = Path(__file__).resolve().parent.parent / ROUTING_FILENAME

# 与改造前仓库里跟踪的内容逐字一致 —— 保证全新部署的行为零变化。
# 注意：值只能是【裸 provider 名】，不能出现 "源:model" 形态，
# 否则会撑坏那些直接读该文件的后台消费方（见 main.py 的 _split_routing_endpoints）。
DEFAULT_ROUTING: Dict[str, Any] = {
    "_default": "minimax",
    "smart_reply": ["minimax", "local"],
    "darwin_eval": "minimax",
    "req_analysis": "minimax",
    "spec_gen": "zhipu",
    "competitive": "minimax",
    "classification": "local",
    "weekly_report": "minimax",
    "monthly_report": "zhipu",
    "reply_supervisor": "zhipu",
    "reply_confidence_scoring": "minimax",
}


def _is_valid(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except Exception:
        return False


def ensure_routing_file(path: Optional[Path] = None) -> bool:
    """缺失或损坏时用默认值生成；已存在且合法则原样不动。

    返回 True 表示本次写了文件。

    ★ 绝不覆盖已有的合法文件 —— 那是运维在设置页改出来的线上配置。
    任何异常都只记日志不抛出：读取方本来就各有兜底，配置初始化失败
    不该把整个启动流程带崩。
    """
    p = Path(path) if path else ROUTING_PATH
    try:
        if p.exists() and _is_valid(p):
            return False
        if p.exists():
            logger.warning("[LLMRouting] %s 内容损坏，用默认值重建", p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_ROUTING, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        logger.info("[LLMRouting] 已用默认值初始化 %s", p)
        return True
    except Exception as exc:
        logger.warning("[LLMRouting] 初始化 %s 失败（不影响启动）: %s", p, exc)
        return False


def load_routing(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取路由配置；文件缺失或损坏时返回默认值而不是空 dict。"""
    p = Path(path) if path else ROUTING_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return dict(DEFAULT_ROUTING)
