"""
chroma 路径工具模块 — 消除各脚本重复的路径解析逻辑。

权威路径来源：APP/backend/main.py:698
  AITICKET_DATA_ROOT 环境变量 → 未设置时 = <repo>/APP/data
  ticket chroma = <data_root>/chroma/ticket

使用示例：
  from _chroma_paths import ticket_chroma_dir, open_ticket_vector_store
  vs = open_ticket_vector_store()  # 禁止裸 VectorStore()
"""

import os
from pathlib import Path
from typing import Optional

# scripts/ → backend/ → APP/ → data/
_SCRIPTS_DIR = Path(__file__).parent
_BACKEND_DIR = _SCRIPTS_DIR.parent
_APP_DIR = _BACKEND_DIR.parent


def ticket_chroma_dir() -> str:
    """返回 ticket chroma 的权威路径。

    与 main.py:698 一致：优先读 AITICKET_DATA_ROOT 环境变量，
    未设置时默认为 APP/data。
    """
    data_root = os.environ.get("AITICKET_DATA_ROOT") or str(_APP_DIR / "data")
    return str(Path(data_root) / "chroma" / "ticket")


def open_ticket_vector_store(allow_download: bool = True):
    """打开 ticket VectorStore，使用权威 persist_directory。

    禁止在各脚本中裸调用 VectorStore()——默认路径是 ./chroma_db，
    与 sync_data.sh / main.py 使用的目录不一致，会导致数据静默写错。
    """
    # 延迟导入，避免模块级副作用
    import sys
    if str(_BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(_BACKEND_DIR))
    from vector_store import VectorStore
    return VectorStore(persist_directory=ticket_chroma_dir(), allow_download=allow_download)
