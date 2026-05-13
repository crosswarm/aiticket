"""
回复内容缓存服务
提供LLM生成回复的缓存机制，避免重复生成
"""

import os
import json
from typing import Optional, Dict
from datetime import datetime, timedelta

# 项目根目录（demo 沙箱可通过 DEMO_RUNTIME_DIR 重定向 data_cache）
BASE_DIR = os.environ.get("DEMO_RUNTIME_DIR") or os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

CACHE_FILE = os.path.join(BASE_DIR, "data_cache", "reply_cache.json")
CACHE_TTL_DAYS = 7  # 缓存有效期7天


def _get_cache_key(issue_key: str, analysis_hash: str = "") -> str:
    """生成缓存键（只用工单号，与ai_analysis内容解耦，TTL控制时效性）"""
    return issue_key


def get_cached_reply(issue_key: str, ai_analysis: Dict) -> Optional[str]:
    """
    获取缓存的回复内容

    Args:
        issue_key: 工单编号
        ai_analysis: AI分析结果（保留参数但不参与缓存键计算）

    Returns:
        缓存的回复内容，不存在或过期返回None
    """
    if not os.path.exists(CACHE_FILE):
        return None

    try:
        cache_key = _get_cache_key(issue_key)

        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)

        entry = cache.get(cache_key)
        if not entry:
            return None

        # 检查是否过期
        cached_time = datetime.fromisoformat(entry['timestamp'])
        if datetime.now() - cached_time > timedelta(days=CACHE_TTL_DAYS):
            return None

        return entry['reply_content']

    except Exception as e:
        print(f"[ReplyCache] 读取缓存失败: {e}")
        return None


def save_cached_reply(issue_key: str, ai_analysis: Dict, reply_content: str):
    """
    保存回复内容到缓存

    Args:
        issue_key: 工单编号
        ai_analysis: AI分析结果（保留参数但不参与缓存键计算）
        reply_content: 生成的回复内容
    """
    try:
        # 确保目录存在
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

        # 加载现有缓存
        cache = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)

        cache_key = _get_cache_key(issue_key)

        # 保存新缓存
        cache[cache_key] = {
            'issue_key': issue_key,
            'reply_content': reply_content,
            'timestamp': datetime.now().isoformat(),
        }

        # 清理过期缓存
        _cleanup_expired_cache(cache)

        tmp = CACHE_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_FILE)

    except Exception as e:
        print(f"[ReplyCache] 保存缓存失败: {e}")


def _cleanup_expired_cache(cache: Dict):
    """清理过期缓存条目"""
    expired_keys = []
    now = datetime.now()

    for key, entry in cache.items():
        try:
            cached_time = datetime.fromisoformat(entry['timestamp'])
            if now - cached_time > timedelta(days=CACHE_TTL_DAYS):
                expired_keys.append(key)
        except:
            # 无法解析时间戳，视为过期
            expired_keys.append(key)

    for key in expired_keys:
        del cache[key]

    # 限制缓存大小（最多保留100条）
    if len(cache) > 100:
        # 按时间戳排序，删除最旧的
        sorted_items = sorted(
            cache.items(),
            key=lambda x: x[1].get('timestamp', ''),
            reverse=True
        )
        cache.clear()
        cache.update(dict(sorted_items[:100]))
