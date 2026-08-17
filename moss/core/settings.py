"""全局键值设置存取(如默认下载目录)。"""
from __future__ import annotations

from moss.common.db import get_session
from moss.model import AppSetting

# 视频画质档位(设置页与分享链接下载共用;原在 main.py,2026-08-17 模块化上移)
QUALITY_CHOICES = {"highest", "1080", "720", "540", "lowest"}


def get_setting(key: str, default: str = "") -> str:
    with get_session() as s:
        row = s.get(AppSetting, key)
        return row.value if row and row.value else default


def set_setting(key: str, value: str):
    with get_session() as s:
        row = s.get(AppSetting, key)
        if row:
            row.value = value
        else:
            row = AppSetting(key=key, value=value)
        s.add(row)
        s.commit()
