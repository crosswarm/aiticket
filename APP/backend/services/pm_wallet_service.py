"""
PM Cookie 钱包：每个用户独立的 PM session 存储

存储路径: data_cache/pm_tokens/{username}.json
格式: {
    "yht_access_token": "...",
    "tenant_info": "0000",
    "extra_cookies": {"ycap_xxx": "..."},
    "uploaded_at": "2026-04-17T10:00:00",
    "expires_at": "2026-04-24T10:00:00"
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WALLET_DIR = Path(__file__).resolve().parent.parent / 'data_cache' / 'pm_tokens'
DEFAULT_TOKEN_PATH = Path(__file__).resolve().parent.parent / 'data_cache' / 'pm_token.json'
EXPIRY_DAYS = 7


class PMNotBoundError(Exception):
    """用户未绑定 PM session 或 session 已过期。"""
    def __init__(self, username: str = ""):
        self.username = username
        super().__init__(f"PM session not bound for user: {username}")


def _ensure_dir() -> None:
    WALLET_DIR.mkdir(parents=True, exist_ok=True)


def save_user_token(username: str, token_data: dict[str, Any]) -> dict[str, Any]:
    _ensure_dir()
    now = datetime.now()
    record = {
        'yht_access_token': token_data.get('yht_access_token', ''),
        'tenant_info': token_data.get('tenant_info', '0000'),
        'extra_cookies': token_data.get('extra_cookies', {}),
        'proxy_endpoint': token_data.get('proxy_endpoint', ''),
        'uploaded_at': now.isoformat(),
        'expires_at': (now + timedelta(days=EXPIRY_DAYS)).isoformat(),
    }
    path = WALLET_DIR / f'{username}.json'
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)
    logger.info(f'[pm_wallet] saved token for {username} (expires {record["expires_at"]})')
    return record


def get_user_token(username: str) -> dict[str, Any] | None:
    path = WALLET_DIR / f'{username}.json'
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    expires = data.get('expires_at', '')
    if expires and datetime.fromisoformat(expires) < datetime.now():
        logger.info(f'[pm_wallet] token expired for {username}')
        path.unlink(missing_ok=True)
        return None
    return data


def get_effective_cookies(username: str | None) -> dict[str, str]:
    """返回用户 PM cookies；用户未绑定时返回空 dict（不降级到管理员 token）。"""
    if username:
        user_data = get_user_token(username)
        if user_data and user_data.get('yht_access_token'):
            cookies = {
                'yht_access_token': user_data['yht_access_token'],
                'tenant_info': user_data.get('tenant_info', '0000'),
            }
            cookies.update(user_data.get('extra_cookies', {}))
            return cookies
    # strict 模式：禁止降级到管理员 token（deployable 无 DEFAULT_TOKEN_PATH，always strict）
    from role_guard import is_strict_role, PMNotBoundError
    if is_strict_role():
        raise PMNotBoundError(username or "")
    return {}


def get_user_proxy(username: str) -> str | None:
    """读取用户的 proxy_endpoint（tailscale IP:port）。"""
    data = get_user_token(username)
    if data and data.get('proxy_endpoint'):
        return data['proxy_endpoint']
    return None


def get_binding_status(username: str) -> dict[str, Any]:
    path = WALLET_DIR / f'{username}.json'
    if not path.is_file():
        return {'bound': False}
    data = get_user_token(username)
    if not data:
        return {'bound': False, 'reason': 'expired'}
    token = data.get('yht_access_token', '')
    if not token:
        return {'bound': False, 'reason': 'no_token'}
    return {
        'bound': True,
        'uploaded_at': data.get('uploaded_at'),
        'expires_at': data.get('expires_at'),
        'token_prefix': token[:8] + '...',
        'proxy_endpoint': data.get('proxy_endpoint', ''),
    }


def list_bindings() -> list[dict[str, Any]]:
    _ensure_dir()
    result = []
    for f in sorted(WALLET_DIR.glob('*.json')):
        username = f.stem
        status = get_binding_status(username)
        status['username'] = username
        result.append(status)
    return result
