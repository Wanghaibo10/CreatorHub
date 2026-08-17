"""引擎内部共享辅助(monitor 与各 Mixin 共用,单独成模块避免循环导入)。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from application.douyin import parse_aweme
from application.douyin.extract import Aweme

log = logging.getLogger("creatorhub.engine")

MAX_AUTO_RETRY = 3

_TZ_COUNTRY = {
    "Asia/Shanghai": "CN", "Asia/Chongqing": "CN", "Asia/Urumqi": "CN",
    "Asia/Hong_Kong": "HK", "Asia/Macau": "MO", "Asia/Taipei": "TW",
}


def _loads(s: str) -> dict:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def _loads_list(s: str) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _danmaku_matches(item: dict, settings: dict) -> bool:
    text = str(item.get("text") or "")
    folded = text.casefold()
    point = max(0, int(item.get("video_time_ms") or 0))
    start_ms = settings.get("time_start_ms", 0)
    end_ms = settings.get("time_end_ms", 0)
    if start_ms and point < start_ms:
        return False
    if end_ms and point > end_ms:
        return False
    includes = settings.get("include_keywords") or []
    excludes = settings.get("exclude_keywords") or []
    if includes and not any(str(k).casefold() in folded for k in includes):
        return False
    if excludes and any(str(k).casefold() in folded for k in excludes):
        return False
    min_len = settings.get("min_text_length", 0)
    max_len = settings.get("max_text_length", 0)
    if min_len and len(text) < min_len:
        return False
    if max_len and len(text) > max_len:
        return False
    if int(item.get("like_count") or 0) < settings.get("min_like_count", 0):
        return False
    return True


def _select_douyin_awemes(items: list, quality: str, first_scan: bool,
                          monitor_since: int, initial_backfill_count: int) -> list[Aweme]:
    """按发布时间稳定排序，并应用“订阅后新增 + 可选首次回填”策略。"""
    parsed = []
    seen = set()
    for item in items:
        aw = parse_aweme(item, quality)
        if not aw or aw.aweme_id in seen:
            continue
        seen.add(aw.aweme_id)
        parsed.append(aw)
    parsed.sort(key=lambda aw: (aw.create_time, aw.aweme_id), reverse=True)

    if not first_scan:
        # create_time 缺失时宁可保留，避免平台字段小改后静默漏掉真正的新作品。
        return [aw for aw in parsed
                if not aw.create_time or aw.create_time >= monitor_since]
    if initial_backfill_count < 0:
        return parsed

    current = [aw for aw in parsed
               if aw.create_time and aw.create_time >= monitor_since]
    historical = [aw for aw in parsed
                  if aw.create_time and aw.create_time < monitor_since]
    return current + historical[:max(0, initial_backfill_count)]


def _douyin_scan_since(monitor_since: int, known_create_times: list[int]) -> int:
    """给旧版残缺首扫留出自愈窗口，但不回退到整个账号历史。"""
    latest_known = max((ts or 0 for ts in known_create_times), default=0)
    return min(monitor_since, latest_known) if latest_known else monitor_since


def _round_robin_by_account(rows: list[tuple[int, int | None]]) \
        -> list[tuple[int, int | None]]:
    """Interleave due rows so one account cannot monopolize a scheduler burst."""
    buckets: dict[object, list[tuple[int, int | None]]] = {}
    for row_id, account_id in rows:
        key: object = account_id if account_id is not None else f"anon:{row_id}"
        buckets.setdefault(key, []).append((row_id, account_id))
    ordered: list[tuple[int, int | None]] = []
    while buckets:
        for key in list(buckets):
            ordered.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return ordered
