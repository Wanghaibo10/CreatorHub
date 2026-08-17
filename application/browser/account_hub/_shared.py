"""跨子模块共用的用户字段识别辅助。

2026-08-17 从 account_hub.py(1930 行)按功能域拆出。
"""
from __future__ import annotations

from moss.common.logging_setup import get_logger

from moss.common.logging_setup import get_logger

log = get_logger("browser.account_hub")


_NAME_KEYS = ("nickname", "nick_name", "user_name", "userName", "name", "nick", "nickName")


_ID_KEYS = ("user_id", "userId", "uid", "id", "red_id", "kwaiId")


_STRONG_ID_KEYS = ("user_id", "userId", "uid", "sec_uid", "secUid", "red_id", "kwaiId")


_AVATAR_KEYS = ("avatar", "avatar_thumb", "avatar_small", "avatar_larger",
                "avatarUrl", "avatar_url", "headurl",
                "head_url", "headUrl", "image", "images", "icon")


def _looks_like_user(d: dict) -> bool:
    """判断是否一个「用户对象」。光有 name+id 不够(JS 模块清单 {id,name} 会误中),
    必须额外带「强用户特征」:user_id/sec_uid 等强 id,或头像。"""
    if not isinstance(d, dict):
        return False
    if not any(d.get(k) for k in _NAME_KEYS):
        return False
    return bool(any(d.get(k) for k in _STRONG_ID_KEYS) or _avatar_of(d))


def _avatar_of(d: dict) -> str:
    for k in _AVATAR_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            ul = v.get("url_list") or v.get("urlList")
            if isinstance(ul, list) and ul:
                return ul[0]
            for kk in ("url", "uri", "url_default"):
                if isinstance(v.get(kk), str) and v[kk].startswith("http"):
                    return v[kk]
        if isinstance(v, list) and v:
            if isinstance(v[0], dict) and v[0].get("url"):
                return v[0]["url"]
            if isinstance(v[0], str) and v[0].startswith("http"):
                return v[0]
    return ""
