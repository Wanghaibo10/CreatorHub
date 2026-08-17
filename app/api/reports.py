"""报表导出 API(/api/reports/*.xlsx)。

从 main.py 抽出(2026-08-17 模块化)。时间窗口/下载响应等辅助在
services/reports.py,xlsx 构建在 app/reporting.py。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import or_, select

from moss.common.db import get_session
from moss.model import (CommentRecord, CommentWatch, ContentRecord, DanmakuRecord, DanmakuWatch, MonitorTarget, ShareDownloadRecord)
from app.service.monitor_meta import _meta_matches, _meta_text
from app.service.reports import (_report_bounds, _report_download, _report_filter_pairs, _report_window)
from app.service.share_history import _share_history_dict

router = APIRouter(tags=["reports"])


@router.get("/api/reports/monitor.xlsx")
async def export_monitor_report(
    platform: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    group_name: str = "",
    tag: str = "",
    data_types: str = "all",
):
    """Export filtered monitoring records as a formatted Excel workbook.

    ``start_date`` and ``end_date`` filter by record collection time.  The
    workbook also keeps each platform timestamp as a separate detail column.
    """
    if start_date and end_date and start_date > end_date:
        raise HTTPException(400, "开始日期不能晚于结束日期")

    requested = {
        item.strip().lower()
        for item in (data_types or "all").split(",")
        if item.strip()
    }
    aliases = {"works": "contents", "content": "contents", "comments": "comments", "comment": "comments", "danmakus": "danmaku"}
    requested = {aliases.get(item, item) for item in requested}
    allowed = {"all", "contents", "comments", "danmaku"}
    if not requested or not requested <= allowed:
        raise HTTPException(400, "data_types 只能是 all、contents、comments、danmaku 的逗号组合")
    include_all = "all" in requested
    include_contents = include_all or "contents" in requested
    include_comments = include_all or "comments" in requested
    include_danmaku = include_all or "danmaku" in requested

    platform = platform.strip() if platform else None
    group_name, tag = _meta_text(group_name, 40), _meta_text(tag, 24)
    window_start = datetime.combine(start_date, time.min) if start_date else None
    # Use an exclusive upper bound so the complete end date is included.
    window_end = (
        datetime.combine(end_date + timedelta(days=1), time.min)
        if end_date else None
    )

    def in_window(statement, model):
        if window_start:
            statement = statement.where(model.created_at >= window_start)
        if window_end:
            statement = statement.where(model.created_at < window_end)
        return statement

    with get_session() as s:
        target_stmt = select(MonitorTarget).order_by(MonitorTarget.id.asc())
        if platform:
            target_stmt = target_stmt.where(MonitorTarget.platform == platform)
        targets = s.exec(target_stmt).all()
        if group_name or tag:
            targets = [t for t in targets if _meta_matches(t, group_name, tag)]
        target_ids = [t.id for t in targets if t.id is not None]

        watch_stmt = select(CommentWatch).order_by(CommentWatch.id.asc())
        danmaku_watch_stmt = select(DanmakuWatch).order_by(DanmakuWatch.id.asc())
        if platform:
            watch_stmt = watch_stmt.where(CommentWatch.platform == platform)
            danmaku_watch_stmt = danmaku_watch_stmt.where(DanmakuWatch.platform == platform)
        watches = s.exec(watch_stmt).all()
        danmaku_watches = s.exec(danmaku_watch_stmt).all()
        if group_name or tag:
            watches = [w for w in watches if _meta_matches(w, group_name, tag)]
            danmaku_watches = [w for w in danmaku_watches if _meta_matches(w, group_name, tag)]
        watch_ids = [w.id for w in watches if w.id is not None]
        danmaku_watch_ids = [w.id for w in danmaku_watches if w.id is not None]

        contents = []
        if include_contents and (not (group_name or tag) or target_ids):
            statement = select(ContentRecord).order_by(
                ContentRecord.created_at.desc(), ContentRecord.id.desc()
            )
            if platform:
                statement = statement.where(ContentRecord.platform == platform)
            if group_name or tag:
                statement = statement.where(ContentRecord.target_id.in_(target_ids))
            statement = in_window(statement, ContentRecord)
            contents = s.exec(statement).all()

        comments = []
        if include_comments and (not (group_name or tag) or watch_ids):
            statement = select(CommentRecord).order_by(
                CommentRecord.created_at.desc(), CommentRecord.id.desc()
            )
            if platform:
                statement = statement.where(CommentRecord.platform == platform)
            if group_name or tag:
                statement = statement.where(CommentRecord.watch_id.in_(watch_ids))
            statement = in_window(statement, CommentRecord)
            comments = s.exec(statement).all()

        danmaku = []
        if include_danmaku and (not (group_name or tag) or danmaku_watch_ids):
            statement = select(DanmakuRecord).order_by(
                DanmakuRecord.created_at.desc(), DanmakuRecord.id.desc()
            )
            if platform:
                statement = statement.where(DanmakuRecord.platform == platform)
            if group_name or tag:
                statement = statement.where(DanmakuRecord.watch_id.in_(danmaku_watch_ids))
            statement = in_window(statement, DanmakuRecord)
            danmaku = s.exec(statement).all()

    from app.service.reporting import REPORT_MIME, build_monitor_report

    payload = build_monitor_report(
        platform=platform or "",
        period_start=start_date,
        period_end=end_date,
        targets=targets,
        contents=contents,
        watches=watches,
        comments=comments,
        danmaku_watches=danmaku_watches,
        danmaku=danmaku,
        generated_at=datetime.now(),
    )
    filename = f"creatorhub_monitor_report_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return StreamingResponse(
        iter([payload]),
        media_type=REPORT_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


@router.get("/api/reports/share-download-history.xlsx")
async def export_share_download_history_report(
    platform: str | None = None,
    q: str = "",
    media_type: str = "",
    status: str = "",
    full: bool = False,
):
    from app.service.reporting import build_share_history_report

    platform = platform.strip() if platform else None
    q, media_type, status = q.strip(), media_type.strip(), status.strip()
    if full:
        q = media_type = status = ""
    if media_type not in {"", "video", "images"}:
        media_type = ""
    if status not in {"", "done", "failed"}:
        status = ""

    with get_session() as s:
        statement = select(ShareDownloadRecord).order_by(
            ShareDownloadRecord.created_at.desc(), ShareDownloadRecord.id.desc()
        )
        if platform:
            statement = statement.where(ShareDownloadRecord.platform == platform)
        if media_type:
            statement = statement.where(ShareDownloadRecord.media_type == media_type)
        if status:
            statement = statement.where(ShareDownloadRecord.status == status)
        source_rows = s.exec(statement).all()

    needle = q.casefold()
    records = []
    for record in source_rows:
        item = _share_history_dict(record)
        if needle:
            searchable = " ".join(
                str(item.get(key) or "")
                for key in ("title", "desc", "author", "item_id", "source_url", "error")
            ).casefold()
            if needle not in searchable:
                continue
        # _share_history_dict serializes this field for the JSON API; keep the
        # datetime object here so Excel receives a real date cell.
        item["created_at"] = record.created_at
        records.append(item)

    payload = build_share_history_report(
        records,
        filters=_report_filter_pairs([
            ("导出范围", "当前平台全部记录" if full else "当前筛选结果"),
            ("平台", platform),
            ("搜索", q),
            ("媒体类型", media_type),
            ("下载状态", status),
        ]),
    )
    return _report_download(payload, "share_download_history")


@router.get("/api/reports/monitors.xlsx")
async def export_monitors_report(
    platform: str | None = None,
    q: str = "",
    group_name: str = "",
    tag: str = "",
    full: bool = False,
):
    from app.service.reporting import build_targets_report

    platform = platform.strip() if platform else None
    q, group_name, tag = q.strip(), _meta_text(group_name, 40), _meta_text(tag, 24)
    if full:
        q = group_name = tag = ""
    with get_session() as s:
        stmt = select(MonitorTarget).order_by(MonitorTarget.id.asc())
        if platform:
            stmt = stmt.where(MonitorTarget.platform == platform)
        targets = s.exec(stmt).all()
        if group_name or tag:
            targets = [t for t in targets if _meta_matches(t, group_name, tag)]
        if q:
            needle = q.casefold()
            targets = [t for t in targets if needle in " ".join(
                [t.alias or "", t.nickname or "", t.keyword or "", t.sec_uid or "",
                 t.group_name or "", t.tags or ""]
            ).casefold()]
        target_ids = [t.id for t in targets if t.id is not None]
        content_stmt = select(ContentRecord)
        if platform:
            content_stmt = content_stmt.where(ContentRecord.platform == platform)
        if group_name or tag or q:
            content_stmt = content_stmt.where(ContentRecord.target_id.in_(target_ids))
        contents = s.exec(content_stmt).all() if target_ids or not (group_name or tag or q) else []

    payload = build_targets_report(
        targets,
        contents,
        filters=_report_filter_pairs([
            ("导出范围", "当前平台全部记录" if full else "当前筛选结果"),
            ("平台", platform), ("搜索", q), ("分组", group_name), ("标签", tag),
        ]),
    )
    return _report_download(payload, "monitors")


@router.get("/api/reports/comment-watches.xlsx")
async def export_comment_watches_report(
    platform: str | None = None,
    q: str = "",
    group_name: str = "",
    tag: str = "",
    full: bool = False,
):
    from app.service.reporting import build_watches_report

    platform = platform.strip() if platform else None
    q, group_name, tag = q.strip(), _meta_text(group_name, 40), _meta_text(tag, 24)
    if full:
        q = group_name = tag = ""
    with get_session() as s:
        stmt = select(CommentWatch).order_by(CommentWatch.id.asc())
        if platform:
            stmt = stmt.where(CommentWatch.platform == platform)
        watches = s.exec(stmt).all()
        if group_name or tag:
            watches = [w for w in watches if _meta_matches(w, group_name, tag)]
        if q:
            needle = q.casefold()
            watches = [w for w in watches if needle in " ".join(
                [w.title or "", w.aweme_id or "", w.sec_uid or "", w.alias or "",
                 w.group_name or "", w.tags or ""]
            ).casefold()]

    payload = build_watches_report(
        watches,
        filters=_report_filter_pairs([
            ("导出范围", "当前平台全部记录" if full else "当前筛选结果"),
            ("平台", platform), ("搜索", q), ("分组", group_name), ("标签", tag),
        ]),
    )
    return _report_download(payload, "comment_watches")


@router.get("/api/reports/danmaku-watches.xlsx")
async def export_danmaku_watches_report(
    platform: str | None = None,
    q: str = "",
    group_name: str = "",
    tag: str = "",
    full: bool = False,
):
    from app.service.reporting import build_danmaku_watches_report

    platform = platform.strip() if platform else None
    q, group_name, tag = q.strip(), _meta_text(group_name, 40), _meta_text(tag, 24)
    if full:
        q = group_name = tag = ""
    with get_session() as s:
        stmt = select(DanmakuWatch).order_by(DanmakuWatch.id.asc())
        if platform:
            stmt = stmt.where(DanmakuWatch.platform == platform)
        watches = s.exec(stmt).all()
        if group_name or tag:
            watches = [w for w in watches if _meta_matches(w, group_name, tag)]
        if q:
            needle = q.casefold()
            watches = [w for w in watches if needle in " ".join(
                [w.title or "", w.aweme_id or "", w.sec_uid or "", w.alias or "",
                 w.group_name or "", w.tags or ""]
            ).casefold()]

    payload = build_danmaku_watches_report(
        watches,
        filters=_report_filter_pairs([
            ("导出范围", "当前平台全部记录" if full else "当前筛选结果"),
            ("平台", platform), ("搜索", q), ("分组", group_name), ("标签", tag),
        ]),
    )
    return _report_download(payload, "danmaku_watches")


@router.get("/api/reports/contents.xlsx")
async def export_contents_report(
    platform: str | None = None,
    target_id: int | None = None,
    group_name: str = "",
    tag: str = "",
    q: str = "",
    media_type: str = "",
    download_status: str = "",
    min_like_count: int = 0,
    min_comment_count: int = 0,
    sort: str = "create_desc",
    start_date: date | None = None,
    end_date: date | None = None,
    full: bool = False,
):
    from app.service.reporting import build_contents_report

    if full:
        target_id = None
        group_name = tag = q = media_type = download_status = ""
        min_like_count = min_comment_count = 0
        sort = "create_desc"
        start = end = None
    else:
        start, end = _report_bounds(start_date, end_date)
    platform = platform.strip() if platform else None
    group_name, tag, q = _meta_text(group_name, 40), _meta_text(tag, 24), q.strip()
    with get_session() as s:
        target_stmt = select(MonitorTarget)
        if platform:
            target_stmt = target_stmt.where(MonitorTarget.platform == platform)
        if target_id is not None:
            target_stmt = target_stmt.where(MonitorTarget.id == target_id)
        targets = s.exec(target_stmt).all()
        eligible_ids = None
        if group_name or tag:
            eligible_ids = [t.id for t in targets if t.id is not None
                            and _meta_matches(t, group_name, tag)]

        stmt = select(ContentRecord)
        if platform:
            stmt = stmt.where(ContentRecord.platform == platform)
        if target_id is not None:
            stmt = stmt.where(ContentRecord.target_id == target_id)
        if eligible_ids is not None:
            stmt = stmt.where(ContentRecord.target_id.in_(eligible_ids))
        if q:
            stmt = stmt.where(or_(ContentRecord.desc.contains(q),
                                  ContentRecord.aweme_id.contains(q)))
        if media_type in ("video", "images"):
            stmt = stmt.where(ContentRecord.media_type == media_type)
        if download_status:
            stmt = stmt.where(ContentRecord.download_status == download_status)
        if min_like_count > 0:
            stmt = stmt.where(ContentRecord.like_count >= min_like_count)
        if min_comment_count > 0:
            stmt = stmt.where(ContentRecord.comment_count >= min_comment_count)
        stmt = _report_window(stmt, ContentRecord, start, end)
        if sort == "create_asc":
            ordering = (ContentRecord.create_time.asc(), ContentRecord.id.asc())
        elif sort == "likes_desc":
            ordering = (ContentRecord.like_count.desc(), ContentRecord.id.desc())
        elif sort == "comments_desc":
            ordering = (ContentRecord.comment_count.desc(), ContentRecord.id.desc())
        else:
            ordering = (ContentRecord.create_time.desc(), ContentRecord.id.desc())
        contents = s.exec(stmt.order_by(*ordering)).all()
        if eligible_ids is not None:
            targets = [t for t in targets if t.id in eligible_ids]

    payload = build_contents_report(
        contents,
        targets,
        filters=_report_filter_pairs([
            ("导出范围", "当前平台全部记录" if full else "当前筛选结果"),
            ("平台", platform), ("来源监控", target_id), ("分组", group_name),
            ("标签", tag), ("搜索", q), ("媒体类型", media_type),
            ("下载状态", download_status), ("最低点赞", min_like_count or ""),
            ("最低评论", min_comment_count or ""), ("排序", sort),
            ("采集开始", start_date.isoformat() if start_date else ""),
            ("采集结束", end_date.isoformat() if end_date else ""),
        ]),
    )
    return _report_download(payload, "contents")


@router.get("/api/reports/comments.xlsx")
async def export_comments_report(
    platform: str | None = None,
    watch_id: int | None = None,
    aweme_id: str | None = None,
    group_name: str = "",
    tag: str = "",
    q: str = "",
    reply_type: str = "",
    min_like_count: int = 0,
    sort: str = "latest",
    start_date: date | None = None,
    end_date: date | None = None,
    full: bool = False,
):
    from app.service.reporting import build_comments_report

    if full:
        watch_id = aweme_id = None
        group_name = tag = q = reply_type = ""
        min_like_count = 0
        sort = "latest"
        start = end = None
    else:
        start, end = _report_bounds(start_date, end_date)
    platform = platform.strip() if platform else None
    group_name, tag, q = _meta_text(group_name, 40), _meta_text(tag, 24), q.strip()
    with get_session() as s:
        watch_stmt = select(CommentWatch)
        if platform:
            watch_stmt = watch_stmt.where(CommentWatch.platform == platform)
        if watch_id is not None:
            watch_stmt = watch_stmt.where(CommentWatch.id == watch_id)
        watches = s.exec(watch_stmt).all()
        eligible_ids = None
        if group_name or tag:
            eligible_ids = [w.id for w in watches if w.id is not None
                            and _meta_matches(w, group_name, tag)]

        stmt = select(CommentRecord)
        if platform:
            stmt = stmt.where(CommentRecord.platform == platform)
        if watch_id is not None:
            stmt = stmt.where(CommentRecord.watch_id == watch_id)
        if aweme_id:
            stmt = stmt.where(CommentRecord.aweme_id == aweme_id)
        if eligible_ids is not None:
            stmt = stmt.where(CommentRecord.watch_id.in_(eligible_ids))
        if q:
            stmt = stmt.where(or_(CommentRecord.text.contains(q),
                                  CommentRecord.user_nickname.contains(q),
                                  CommentRecord.aweme_id.contains(q)))
        if reply_type == "top":
            stmt = stmt.where(CommentRecord.reply_to == "")
        elif reply_type == "reply":
            stmt = stmt.where(CommentRecord.reply_to != "")
        if min_like_count > 0:
            stmt = stmt.where(CommentRecord.like_count >= min_like_count)
        stmt = _report_window(stmt, CommentRecord, start, end)
        if sort == "oldest":
            ordering = (CommentRecord.create_time.asc(), CommentRecord.id.asc())
        elif sort == "likes_desc":
            ordering = (CommentRecord.like_count.desc(), CommentRecord.id.desc())
        else:
            ordering = (CommentRecord.create_time.desc(), CommentRecord.id.desc())
        comments = s.exec(stmt.order_by(*ordering)).all()
        if eligible_ids is not None:
            watches = [w for w in watches if w.id in eligible_ids]

    payload = build_comments_report(
        comments,
        watches,
        filters=_report_filter_pairs([
            ("导出范围", "当前平台全部记录" if full else "当前筛选结果"),
            ("平台", platform), ("评论监控", watch_id), ("作品ID", aweme_id),
            ("分组", group_name), ("标签", tag), ("搜索", q),
            ("评论类型", reply_type), ("最低点赞", min_like_count or ""),
            ("排序", sort), ("采集开始", start_date.isoformat() if start_date else ""),
            ("采集结束", end_date.isoformat() if end_date else ""),
        ]),
    )
    return _report_download(payload, "comments")


@router.get("/api/reports/danmaku.xlsx")
async def export_danmaku_report(
    platform: str | None = None,
    watch_id: int | None = None,
    aweme_id: str | None = None,
    group_name: str = "",
    tag: str = "",
    q: str = "",
    min_video_time_ms: int = 0,
    max_video_time_ms: int = 0,
    min_like_count: int = 0,
    sort: str = "video_asc",
    start_date: date | None = None,
    end_date: date | None = None,
    full: bool = False,
):
    from app.service.reporting import build_danmaku_report

    if full:
        watch_id = aweme_id = None
        group_name = tag = q = ""
        min_video_time_ms = max_video_time_ms = min_like_count = 0
        sort = "video_asc"
        start = end = None
    else:
        start, end = _report_bounds(start_date, end_date)
    platform = platform.strip() if platform else None
    group_name, tag, q = _meta_text(group_name, 40), _meta_text(tag, 24), q.strip()
    with get_session() as s:
        watch_stmt = select(DanmakuWatch)
        if platform:
            watch_stmt = watch_stmt.where(DanmakuWatch.platform == platform)
        if watch_id is not None:
            watch_stmt = watch_stmt.where(DanmakuWatch.id == watch_id)
        watches = s.exec(watch_stmt).all()
        eligible_ids = None
        if group_name or tag:
            eligible_ids = [w.id for w in watches if w.id is not None
                            and _meta_matches(w, group_name, tag)]

        stmt = select(DanmakuRecord)
        if platform:
            stmt = stmt.where(DanmakuRecord.platform == platform)
        if watch_id is not None:
            stmt = stmt.where(DanmakuRecord.watch_id == watch_id)
        if aweme_id:
            stmt = stmt.where(DanmakuRecord.aweme_id == aweme_id)
        if eligible_ids is not None:
            stmt = stmt.where(DanmakuRecord.watch_id.in_(eligible_ids))
        if q:
            stmt = stmt.where(or_(DanmakuRecord.text.contains(q),
                                  DanmakuRecord.user_id.contains(q),
                                  DanmakuRecord.user_nickname.contains(q)))
        if min_video_time_ms > 0:
            stmt = stmt.where(DanmakuRecord.video_time_ms >= min_video_time_ms)
        if max_video_time_ms > 0:
            stmt = stmt.where(DanmakuRecord.video_time_ms <= max_video_time_ms)
        if min_like_count > 0:
            stmt = stmt.where(DanmakuRecord.like_count >= min_like_count)
        stmt = _report_window(stmt, DanmakuRecord, start, end)
        if sort == "video_desc":
            ordering = (DanmakuRecord.video_time_ms.desc(), DanmakuRecord.id.desc())
        elif sort == "captured_asc":
            ordering = (DanmakuRecord.created_at.asc(), DanmakuRecord.id.asc())
        elif sort == "captured_desc":
            ordering = (DanmakuRecord.created_at.desc(), DanmakuRecord.id.desc())
        else:
            ordering = (DanmakuRecord.video_time_ms.asc(), DanmakuRecord.id.asc())
        danmaku = s.exec(stmt.order_by(*ordering)).all()
        if eligible_ids is not None:
            watches = [w for w in watches if w.id in eligible_ids]

    payload = build_danmaku_report(
        danmaku,
        watches,
        filters=_report_filter_pairs([
            ("导出范围", "当前平台全部记录" if full else "当前筛选结果"),
            ("平台", platform), ("弹幕监控", watch_id), ("作品ID", aweme_id),
            ("分组", group_name), ("标签", tag), ("搜索", q),
            ("视频内起点(ms)", min_video_time_ms or ""),
            ("视频内终点(ms)", max_video_time_ms or ""),
            ("最低点赞", min_like_count or ""), ("排序", sort),
            ("采集开始", start_date.isoformat() if start_date else ""),
            ("采集结束", end_date.isoformat() if end_date else ""),
        ]),
    )
    return _report_download(payload, "danmaku")
