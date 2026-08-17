"""评论/弹幕记录的序列化与存量修复。

从 main.py 抽出(2026-08-17 模块化):watches 路由与 account-works 域共用
序列化;_backfill_danmaku_records 由 lifespan 启动时调用。
"""
from __future__ import annotations

import json

from sqlmodel import select

from moss.common.db import get_session
from moss.model import CommentRecord, DanmakuRecord
from application.douyin import parse_danmaku


def _comment_dict(c: CommentRecord) -> dict:
    return {
        "id": c.id, "watch_id": c.watch_id, "aweme_id": c.aweme_id,
        "comment_id": c.comment_id, "text": c.text, "user_nickname": c.user_nickname,
        "like_count": c.like_count, "create_time": c.create_time,
        "is_reply": bool(c.reply_to),
    }


def _parse_stored_danmaku(row: DanmakuRecord) -> dict | None:
    if not row.raw_json:
        return None
    try:
        raw = json.loads(row.raw_json)
    except Exception:
        return None
    return parse_danmaku(raw, row.aweme_id or "")


def _backfill_danmaku_records() -> int:
    """用 raw_json 修复接口改版前已入库的 offset_time/user_id 等字段。"""
    repaired = 0
    with get_session() as s:
        rows = s.exec(select(DanmakuRecord)).all()
        for row in rows:
            parsed = _parse_stored_danmaku(row)
            if not parsed:
                continue
            changed = False
            for name in ("aweme_id", "user_id", "user_nickname", "video_time_ms",
                         "create_time", "like_count", "is_blocked"):
                current = getattr(row, name)
                value = parsed.get(name)
                if (not current) and value not in (None, "", 0, False):
                    setattr(row, name, value)
                    changed = True
            if changed:
                s.add(row)
                repaired += 1
        if repaired:
            s.commit()
    return repaired


def _danmaku_dict(row: DanmakuRecord) -> dict:
    parsed = _parse_stored_danmaku(row)
    user_id = row.user_id or (parsed or {}).get("user_id", "")
    user_nickname = row.user_nickname or (parsed or {}).get("user_nickname", "")
    point = max(0, int(row.video_time_ms or (parsed or {}).get("video_time_ms", 0) or 0))
    return {
        "id": row.id, "watch_id": row.watch_id, "aweme_id": row.aweme_id,
        "danmaku_id": row.danmaku_id, "text": row.text,
        "user_id": user_id, "user_nickname": user_nickname,
        "video_time_ms": point, "video_time": point / 1000,
        "create_time": row.create_time, "like_count": row.like_count,
        "is_blocked": row.is_blocked, "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
