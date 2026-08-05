"""pytest 全局夹具与导入路径设置。

存在的意义：在此之前 32 个测试文件里有 29 个各写一遍 `sys.path.insert`，
且有 5 种不同写法。新增测试不必再抄这段样板 —— 直接 `import auth_service` 即可。
（存量文件里的 insert 是幂等的，保留不动，避免为零功能收益改 29 个文件。）

约束：本文件只用标准库。测试栈必须能在【离线的 172】上跑起来，
不能引入任何需要联网安装的东西。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent

# APP/backend 与 APP/backend/scripts 都要可导入：
# 前者供 `import auth_service` / `from services... import`，
# 后者因为 scripts/ 下有模块用裸 import 互相引用（如 incremental_issues_index → _chroma_paths）。
for _p in (BACKEND_DIR, BACKEND_DIR / "scripts"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


def pytest_configure(config):
    """把测试运行标记出来，供被测代码在需要时避开真实副作用。"""
    os.environ.setdefault("AITICKET_TESTING", "1")
