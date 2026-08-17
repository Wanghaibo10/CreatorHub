"""已下载作品 API(/api/contents、/api/stats/series)。

从 main.py 抽出(2026-08-17 模块化)。序列化与本地文件在
services/content_data.py,文件管理器操作在 services/local_files.py。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlmodel import select

from moss.common.db import get_session
from moss.model import CommentRecord, ContentRecord, MonitorTarget
from moss.core.runtime import rt
from app.service.content_data import (_content_dict, _content_local_media_path, _content_local_path, _delete_content_files)
from app.service.local_files import _require_local_action, _reveal_in_file_manager
from app.service.monitor_meta import _meta_matches, _meta_text

router = APIRouter(tags=["contents"])

@router.get("/api/contents")
async def all_contents(limit: int = 100, platform: str | None = None,
                       target_id: int | None = None, group_name: str = "",
                       tag: str = "", q: str = "", media_type: str = "",
                       download_status: str = "", min_like_count: int = 0,
                       min_comment_count: int = 0, sort: str = "create_desc",
                       page: int = 1, page_size: int = 10,
                       paginate: bool = False):
    """Return monitored works, optionally as a filtered paginated result.

    The legacy list response remains the default for older callers.  The web
    UI opts into the object response with ``paginate=true`` so it can show a
    stable total while filters are applied in SQL rather than in the browser.
    """
    limit = max(1, min(limit, 1000))
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    with get_session() as s:
        stmt = select(ContentRecord)
        if platform:
            stmt = stmt.where(ContentRecord.platform == platform)
        if target_id is not None:
            stmt = stmt.where(ContentRecord.target_id == target_id)
        text_query = q.strip()
        if text_query:
            stmt = stmt.where(or_(ContentRecord.desc.contains(text_query),
                                  ContentRecord.aweme_id.contains(text_query)))
        if media_type in ("video", "images"):
            stmt = stmt.where(ContentRecord.media_type == media_type)
        if download_status:
            stmt = stmt.where(ContentRecord.download_status == download_status)
        if min_like_count > 0:
            stmt = stmt.where(ContentRecord.like_count >= min_like_count)
        if min_comment_count > 0:
            stmt = stmt.where(ContentRecord.comment_count >= min_comment_count)
        group_name, tag = _meta_text(group_name, 40), _meta_text(tag, 24)
        if group_name or tag:
            target_query = select(MonitorTarget)
            if platform:
                target_query = target_query.where(MonitorTarget.platform == platform)
            targets = s.exec(target_query).all()
            eligible_ids = [t.id for t in targets if t.id is not None
                            and _meta_matches(t, group_name, tag)]
            if not eligible_ids:
                if not paginate:
                    return []
                return {"items": [], "total": 0, "page": page,
                        "page_size": page_size, "pages": 1,
                        "has_prev": page > 1, "has_next": False}
            stmt = stmt.where(ContentRecord.target_id.in_(eligible_ids))

        if sort == "create_asc":
            ordering = (ContentRecord.create_time.asc(), ContentRecord.id.asc())
        elif sort == "likes_desc":
            ordering = (ContentRecord.like_count.desc(), ContentRecord.id.desc())
        elif sort == "comments_desc":
            ordering = (ContentRecord.comment_count.desc(), ContentRecord.id.desc())
        else:
            # 按作品发布时间倒序(回填时多条同批入库,用 id 排序会乱;create_time 才是真实时间序)
            ordering = (ContentRecord.create_time.desc(), ContentRecord.id.desc())
        if not paginate:
            rows = s.exec(stmt.order_by(*ordering).limit(limit)).all()
            return [_content_dict(r) for r in rows]

        total = int(s.exec(select(func.count()).select_from(stmt.subquery())).one())
        pages = max(1, (total + page_size - 1) // page_size)
        rows = s.exec(stmt.order_by(*ordering)
                      .offset((page - 1) * page_size)
                      .limit(page_size)).all()
        return {
            "items": [_content_dict(r) for r in rows],
            "total": total, "page": page, "page_size": page_size,
            "pages": pages, "has_prev": page > 1, "has_next": page < pages,
        }


@router.get("/api/stats/series")
async def stats_series(platform: str | None = None, days: int = 7):
    """近 N 天每天采集到的新作品 / 新评论计数(按入库时间 created_at 分桶),供总览图表用。"""
    from datetime import timedelta
    days = max(1, min(days, 31))
    today = datetime.utcnow().date()
    labels = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    index = {d: i for i, d in enumerate(labels)}

    def bucket(model) -> list[int]:
        counts = [0] * days
        with get_session() as s:
            q = select(model.created_at)
            if platform:
                q = q.where(model.platform == platform)
            for ts in s.exec(q).all():
                if not ts:
                    continue
                key = (ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10])
                i = index.get(key)
                if i is not None:
                    counts[i] += 1
        return counts

    return {"days": labels, "contents": bucket(ContentRecord),
            "comments": bucket(CommentRecord)}


@router.get("/api/contents/{cid}/media")
async def content_media(cid: int):
    """返回一条作品/笔记的媒体直链列表,供前端预览(图集/视频)。"""
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        try:
            medias = json.loads(rec.media_json or "[]")
        except Exception:
            medias = []
        local_media = _content_local_media_path(rec)
        return {
            "id": rec.id, "platform": rec.platform, "desc": rec.desc,
            "media_type": rec.media_type, "cover_url": rec.cover_url,
            "local_path": rec.local_path, "medias": medias,
            "local_url": f"/api/contents/{rec.id}/local-media" if local_media else "",
        }


@router.api_route("/api/contents/{cid}/local-media", methods=["GET", "HEAD"])
async def content_local_media(cid: int):
    """Stream downloaded media from the recorded path, including HTTP Range support."""
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        path = _content_local_media_path(rec)
    if not path:
        raise HTTPException(404, "本地媒体不存在")
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache"},
    )


@router.post("/api/contents/{cid}/reveal")
async def reveal_content_file(cid: int, request: Request):
    """在服务所在电脑的文件管理器中打开本地目录或定位文件。"""
    _require_local_action(request)
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        path = _content_local_path(rec)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(_reveal_in_file_manager, path)
    except OSError as e:
        raise HTTPException(500, f"打开文件夹失败:{e}") from e
    return {"ok": True}


@router.post("/api/contents/{cid}/retry-download")
async def retry_download(cid: int):
    if not rt.engine:
        raise HTTPException(503, "引擎未就绪")
    return await rt.engine.retry_download(cid)


@router.delete("/api/contents/{cid}")
async def del_content(cid: int, with_file: bool = True):
    removed = 0
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        if with_file:
            removed = _delete_content_files(rec)
        s.delete(rec); s.commit()
    return {"ok": True, "files_removed": removed}


class IdsIn(BaseModel):
    ids: list[int]
    with_file: bool = True


@router.post("/api/contents/batch-delete")
async def batch_del_contents(body: IdsIn):
    deleted = removed = 0
    with get_session() as s:
        for cid in body.ids:
            rec = s.get(ContentRecord, cid)
            if not rec:
                continue
            if body.with_file:
                removed += _delete_content_files(rec)
            s.delete(rec); deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted, "files_removed": removed}
