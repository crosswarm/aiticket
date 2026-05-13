"""
auth_deps — FastAPI 依赖函数，供 main.py 与各 router 共用。
不引入任何业务逻辑，只读 request.state.current_user。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request


def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    return getattr(request.state, "current_user", None)


def require_authenticated_user(request: Request) -> Dict[str, Any]:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin_user(request: Request) -> Dict[str, Any]:
    user = require_authenticated_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
