"""
host_context — 双主机部署环境标识与 peer bridge 代理

环境变量：
  AITICKET_HOST=mini|qcl  (默认 mini)
  AITICKET_PEER_BRIDGE    (例：http://127.0.0.1:13800)

用法：
  from services.host_context import HOST, PEER_BRIDGE_URL, proxy_to_peer
"""
from __future__ import annotations

import os

HOST: str = os.environ.get("AITICKET_HOST", "mini").lower()
PEER_BRIDGE_URL: str | None = os.environ.get("AITICKET_PEER_BRIDGE")


def is_mini() -> bool:
    return HOST != "qcl"


def is_qcl() -> bool:
    return HOST == "qcl"


def proxy_to_peer(path: str, method: str = "POST", **kw) -> dict:
    """将请求转发到对端主机；对端不可达时返回 503。"""
    import requests
    from fastapi import HTTPException
    if not PEER_BRIDGE_URL:
        raise HTTPException(503, "Peer bridge not configured (AITICKET_PEER_BRIDGE unset)")
    url = f"{PEER_BRIDGE_URL.rstrip('/')}{path}"
    try:
        r = requests.request(method, url, timeout=15, **kw)
        r.raise_for_status()
        return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Peer bridge unreachable: {exc}")
