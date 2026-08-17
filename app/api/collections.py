"""关键词批量采集 API(/api/collections,当前版本:抖音)。

从 main.py 抽出(2026-08-17 模块化)。采集执行在 engine/monitor.py,
xlsx 构建在 app/reporting.py,文件管理器操作在 services/local_files.py。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import func
from sqlmodel import select

from moss.common.db import get_session
from moss.model import (DouyinAccount, KeywordCollectionComment, KeywordCollectionContent, KeywordCollectionJob)
from moss.core.runtime import rt
from app.service.local_files import (_open_local_path, _require_local_action, _reveal_in_file_manager)
from app.service.reports import _report_download
from moss.core.settings import QUALITY_CHOICES

router = APIRouter(tags=["collections"])


class KeywordCollectionIn(BaseModel):
    platform: str = "douyin"
    account_id: int
    keywords: list[str] = PydanticField(default_factory=list)
    max_contents_per_keyword: int = 20
    max_comments_per_content: int = 20
    include_replies: bool = False
    download_media: bool = False
    video_quality: str = "highest"
    download_dir: str = ""


def _validated_collection_input(body: KeywordCollectionIn) \
        -> tuple[str, list[str], str, str]:
    """校验创建/编辑共用的任务配置并返回规范化值。"""
    platform = body.platform.strip().lower()
    if platform != "douyin":
        raise HTTPException(400, "当前版本关键词批量采集仅支持抖音")
    keywords = _collection_keywords(body.keywords)
    if not keywords:
        raise HTTPException(400, "请至少填写一个关键词")
    if len(keywords) > 20:
        raise HTTPException(400, "单个任务最多包含 20 个关键词")
    if any(len(value) > 80 for value in keywords):
        raise HTTPException(400, "单个关键词不能超过 80 个字符")
    if not 1 <= body.max_contents_per_keyword <= 100:
        raise HTTPException(400, "每个关键词作品数须为 1~100")
    if not 0 <= body.max_comments_per_content <= 200:
        raise HTTPException(400, "每个作品评论数须为 0~200")
    quality = body.video_quality.strip() or "highest"
    if quality not in QUALITY_CHOICES:
        raise HTTPException(400, f"画质取值无效: {quality}")
    download_dir = body.download_dir.strip()
    if download_dir:
        try:
            Path(download_dir).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise HTTPException(400, f"下载目录不可用: {exc}")
    return platform, keywords, quality, download_dir


def _collection_keywords(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        # API 调用者也可把多行或逗号分隔内容放进一个数组项。
        expanded = str(raw or "").replace("，", ",").replace("\r", "\n")
        for line in expanded.replace("\n", ",").split(","):
            value = line.strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                out.append(value)
    return out


def _collection_error_for_display(value: str) -> str:
    """把采集异常压缩成适合页面展示的短文案，原始值仍保留供诊断/导出。"""
    output = []
    for raw_line in str(value or "").splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        lowered = line.lower()
        if "targetclosederror" in lowered or "has been closed" in lowered:
            keyword = line.split(":", 1)[0].strip()
            prefix = f"{keyword}：" if keyword else ""
            line = f"{prefix}采集窗口已关闭，请点击“续跑”并保持窗口开启"
        elif len(line) > 220:
            line = line[:219].rstrip() + "…"
        output.append(line)
    return "\n".join(output[-8:])


def _collection_job_dict(job: KeywordCollectionJob) -> dict:
    try:
        keywords = json.loads(job.keywords or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        keywords = []
    planned = len(keywords) * max(0, job.max_contents_per_keyword)
    return {
        "id": job.id, "platform": job.platform, "account_id": job.account_id,
        "keywords": keywords,
        "max_contents_per_keyword": job.max_contents_per_keyword,
        "max_comments_per_content": job.max_comments_per_content,
        "include_replies": job.include_replies,
        "download_media": job.download_media,
        "video_quality": job.video_quality,
        "download_dir": job.download_dir,
        "status": job.status, "current_keyword": job.current_keyword,
        "current_step": job.current_step,
        "content_count": job.content_count, "comment_count": job.comment_count,
        "planned_content_count": planned, "error_count": job.error_count,
        "error": _collection_error_for_display(job.error),
        "error_detail": job.error,
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


_COLLECTION_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
_COLLECTION_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
_COLLECTION_MEDIA_EXTS = _COLLECTION_IMAGE_EXTS | _COLLECTION_VIDEO_EXTS


def _collection_remote_medias(row: KeywordCollectionContent) -> list[dict]:
    """Return normalized remote media records saved by the collector."""
    try:
        raw_items = json.loads(row.media_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_items = []
    if not isinstance(raw_items, list):
        return []
    medias = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        ext = str(item.get("ext") or "").strip().lower().lstrip(".")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"image", "video"}:
            kind = "image" if f".{ext}" in _COLLECTION_IMAGE_EXTS else "video"
        medias.append({"url": url, "kind": kind, "ext": ext})
    return medias


def _collection_local_path(row: KeywordCollectionContent) -> Path | None:
    if not row.local_path:
        return None
    try:
        path = Path(row.local_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() or path.is_dir() else None


def _collection_media_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"_(\d+)\.[^.]+$", path.name)
    return (int(match.group(1)) if match else -1, path.name.casefold())


def _collection_local_media_paths(row: KeywordCollectionContent) -> list[Path]:
    """Resolve downloaded media without mixing files from another work."""
    path = _collection_local_path(row)
    if not path:
        return []
    if path.is_file():
        try:
            return [path] if path.suffix.lower() in _COLLECTION_MEDIA_EXTS and path.stat().st_size > 0 else []
        except OSError:
            return []
    prefix = f"{row.aweme_id}_"
    try:
        candidates = [
            child for child in path.iterdir()
            if child.is_file() and child.name.startswith(prefix)
            and child.suffix.lower() in _COLLECTION_MEDIA_EXTS
            and child.stat().st_size > 0
        ]
    except OSError:
        return []
    return sorted(candidates, key=_collection_media_sort_key)


def _collection_content_dict(row: KeywordCollectionContent) -> dict:
    url = (f"https://www.xiaohongshu.com/explore/{row.aweme_id}"
           if row.platform == "xhs"
           else f"https://www.douyin.com/video/{row.aweme_id}")
    local_files = _collection_local_media_paths(row)
    remote_medias = _collection_remote_medias(row)
    try:
        file_size = sum(path.stat().st_size for path in local_files)
    except OSError:
        file_size = 0
    return {
        "id": row.id, "job_id": row.job_id, "platform": row.platform,
        "keyword": row.keyword, "aweme_id": row.aweme_id, "desc": row.desc,
        "author_name": row.author_name, "author_id": row.author_id,
        "media_type": row.media_type, "create_time": row.create_time,
        "cover_url": row.cover_url, "like_count": row.like_count,
        "comment_count": row.comment_count,
        "collected_comment_count": row.collected_comment_count,
        "download_status": row.download_status, "local_path": row.local_path,
        "error": row.error, "url": url,
        "local_exists": bool(local_files),
        "media_count": len(local_files) or len(remote_medias),
        "file_size": file_size,
        "preview_available": bool(local_files or remote_medias or row.cover_url),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _collection_comment_dict(row: KeywordCollectionComment) -> dict:
    return {
        "id": row.id, "job_id": row.job_id, "content_id": row.content_id,
        "aweme_id": row.aweme_id, "comment_id": row.comment_id,
        "text": row.text, "user_nickname": row.user_nickname,
        "like_count": row.like_count, "create_time": row.create_time,
        "reply_to": row.reply_to,
    }


@router.post("/api/collections")
async def create_keyword_collection(body: KeywordCollectionIn):
    platform, keywords, quality, download_dir = _validated_collection_input(body)
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if (not account or account.platform != platform
                or account.status != "active" or not account.storage_state):
            raise HTTPException(400, "所选账号不存在、登录态失效或与平台不匹配")
        job = KeywordCollectionJob(
            platform=platform, account_id=body.account_id,
            keywords=json.dumps(keywords, ensure_ascii=False),
            max_contents_per_keyword=body.max_contents_per_keyword,
            max_comments_per_content=body.max_comments_per_content,
            include_replies=body.include_replies,
            download_media=body.download_media,
            video_quality=quality, download_dir=download_dir,
        )
        session.add(job); session.commit(); session.refresh(job)
        payload = _collection_job_dict(job)
    if engine:
        rt.engine.enqueue_collection_job(job.id)
    return payload


@router.put("/api/collections/{job_id}")
async def update_keyword_collection(job_id: int, body: KeywordCollectionIn):
    """修改已结束任务的配置；历史作品/评论保留，续跑时按任务去重。"""
    platform, keywords, quality, download_dir = _validated_collection_input(body)
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        if job.platform != "douyin":
            raise HTTPException(400, "当前版本仅支持编辑抖音采集任务")
        if job.status in {"pending", "running"}:
            raise HTTPException(409, "等待或执行中的任务请先取消，再编辑配置")
        account = session.get(DouyinAccount, body.account_id)
        if (not account or account.platform != platform
                or account.status != "active" or not account.storage_state):
            raise HTTPException(400, "所选账号不存在、登录态失效或与平台不匹配")

        job.account_id = body.account_id
        job.keywords = json.dumps(keywords, ensure_ascii=False)
        job.max_contents_per_keyword = body.max_contents_per_keyword
        job.max_comments_per_content = body.max_comments_per_content
        job.include_replies = body.include_replies
        job.download_media = body.download_media
        job.video_quality = quality
        job.download_dir = download_dir
        job.current_keyword = ""
        job.current_step = "配置已更新，可点击续跑"
        job.cancel_requested = False
        session.add(job); session.commit(); session.refresh(job)
        return _collection_job_dict(job)


@router.get("/api/collections")
async def list_keyword_collections(platform: str | None = None, limit: int = 100):
    limit = max(1, min(limit, 300))
    with get_session() as session:
        stmt = select(KeywordCollectionJob)
        if platform in {"douyin", "xhs"}:
            stmt = stmt.where(KeywordCollectionJob.platform == platform)
        rows = session.exec(
            stmt.order_by(KeywordCollectionJob.created_at.desc()).limit(limit)).all()
        return [_collection_job_dict(row) for row in rows]


@router.get("/api/collections/{job_id}")
async def get_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        return _collection_job_dict(job)


@router.get("/api/collections/{job_id}/contents")
async def list_keyword_collection_contents(job_id: int, keyword: str = "",
                                           page: int = 1, page_size: int = 20):
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    with get_session() as session:
        if not session.get(KeywordCollectionJob, job_id):
            raise HTTPException(404, "采集任务不存在")
        filters = [KeywordCollectionContent.job_id == job_id]
        if keyword:
            filters.append(KeywordCollectionContent.keyword == keyword)
        count_stmt = select(func.count(KeywordCollectionContent.id)).where(*filters)
        total = int(session.exec(count_stmt).one() or 0)
        rows = session.exec(
            select(KeywordCollectionContent).where(*filters)
            .order_by(KeywordCollectionContent.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
        return {
            "items": [_collection_content_dict(row) for row in rows],
            "page": page, "page_size": page_size, "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
        }


def _get_collection_content(session, job_id: int,
                            content_id: int) -> KeywordCollectionContent:
    row = session.get(KeywordCollectionContent, content_id)
    if not row or row.job_id != job_id:
        raise HTTPException(404, "采集作品不存在")
    return row


@router.get("/api/collections/{job_id}/contents/{content_id}/media")
async def keyword_collection_content_media(job_id: int, content_id: int):
    """Return local-first media URLs for the collection result preview."""
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        local_paths = _collection_local_media_paths(row)
        remote_medias = _collection_remote_medias(row)
        local_medias = [{
            "kind": "image" if path.suffix.lower() in _COLLECTION_IMAGE_EXTS else "video",
            "url": f"/api/collections/{job_id}/contents/{content_id}/local-media/{index}",
        } for index, path in enumerate(local_paths)]
        local_video = next(
            (item for item in local_medias if item["kind"] == "video"), None)
        has_local_images = any(item["kind"] == "image" for item in local_medias)
        medias = local_medias if has_local_images else remote_medias
        return {
            "id": row.id, "platform": row.platform, "desc": row.desc,
            "media_type": row.media_type, "cover_url": row.cover_url,
            "local_path": row.local_path, "medias": medias,
            "local_url": local_video["url"] if local_video else "",
            "source_url": (f"https://www.xiaohongshu.com/explore/{row.aweme_id}"
                           if row.platform == "xhs"
                           else f"https://www.douyin.com/video/{row.aweme_id}"),
        }


@router.api_route(
    "/api/collections/{job_id}/contents/{content_id}/local-media/{media_index}",
    methods=["GET", "HEAD"],
)
async def keyword_collection_local_media(job_id: int, content_id: int,
                                         media_index: int):
    """Stream a downloaded collection media file for inline browser preview."""
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        paths = _collection_local_media_paths(row)
    if media_index < 0 or media_index >= len(paths):
        raise HTTPException(404, "本地媒体不存在")
    path = paths[media_index]
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache"},
    )


@router.post("/api/collections/{job_id}/contents/{content_id}/reveal")
async def reveal_keyword_collection_content(job_id: int, content_id: int,
                                            request: Request):
    """Reveal a downloaded collection file (or its gallery directory)."""
    _require_local_action(request)
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        path = _collection_local_path(row)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(_reveal_in_file_manager, path)
    except OSError as exc:
        raise HTTPException(500, f"打开文件夹失败：{exc}") from exc
    return {"ok": True}


@router.post("/api/collections/{job_id}/contents/{content_id}/open")
async def open_keyword_collection_content(job_id: int, content_id: int,
                                          request: Request):
    """Open the downloaded media in the operating system's default application."""
    _require_local_action(request, "open")
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        media_paths = _collection_local_media_paths(row)
        path = media_paths[0] if media_paths else _collection_local_path(row)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(_open_local_path, path)
    except OSError as exc:
        raise HTTPException(500, f"打开文件失败：{exc}") from exc
    return {"ok": True}


@router.get("/api/collections/{job_id}/comments")
async def list_keyword_collection_comments(job_id: int, content_id: int,
                                           limit: int = 300):
    limit = max(1, min(limit, 1000))
    with get_session() as session:
        content = session.get(KeywordCollectionContent, content_id)
        if not content or content.job_id != job_id:
            raise HTTPException(404, "采集作品不存在")
        rows = session.exec(
            select(KeywordCollectionComment)
            .where(KeywordCollectionComment.job_id == job_id)
            .where(KeywordCollectionComment.content_id == content_id)
            .order_by(KeywordCollectionComment.create_time.desc())
            .limit(limit)
        ).all()
        return [_collection_comment_dict(row) for row in rows]


@router.post("/api/collections/{job_id}/cancel")
async def cancel_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        if job.status in {"done", "partial", "failed", "canceled"}:
            return _collection_job_dict(job)
        job.cancel_requested = True
        if job.status == "pending":
            job.status = "canceled"
            job.current_step = "已取消"
            job.finished_at = datetime.utcnow()
        else:
            job.current_step = "正在安全停止"
        session.add(job); session.commit(); session.refresh(job)
        return _collection_job_dict(job)


@router.post("/api/collections/{job_id}/retry")
async def retry_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        if job.status in {"pending", "running"}:
            raise HTTPException(409, "任务仍在等待或执行中")
        job.status = "pending"
        job.current_keyword = ""
        job.current_step = "等待继续"
        job.error_count = 0
        job.error = ""
        job.cancel_requested = False
        job.started_at = None
        job.finished_at = None
        session.add(job); session.commit(); session.refresh(job)
        payload = _collection_job_dict(job)
    if engine:
        rt.engine.enqueue_collection_job(job_id)
    return payload


@router.delete("/api/collections/{job_id}")
async def delete_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            return {"ok": True, "deleted": 0}
        if job.status == "running":
            raise HTTPException(409, "请先取消正在执行的任务")
        for row in session.exec(
                select(KeywordCollectionComment)
                .where(KeywordCollectionComment.job_id == job_id)).all():
            session.delete(row)
        for row in session.exec(
                select(KeywordCollectionContent)
                .where(KeywordCollectionContent.job_id == job_id)).all():
            session.delete(row)
        session.delete(job); session.commit()
    return {"ok": True, "deleted": 1}


@router.get("/api/collections/{job_id}/export.xlsx")
async def export_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        contents = session.exec(
            select(KeywordCollectionContent)
            .where(KeywordCollectionContent.job_id == job_id)
            .order_by(KeywordCollectionContent.keyword,
                      KeywordCollectionContent.create_time.desc())).all()
        comments = session.exec(
            select(KeywordCollectionComment)
            .where(KeywordCollectionComment.job_id == job_id)
            .order_by(KeywordCollectionComment.aweme_id,
                      KeywordCollectionComment.create_time.desc())).all()
        from app.service.reporting import build_keyword_collection_report
        payload = build_keyword_collection_report(job, contents, comments)
    return _report_download(payload, f"keyword-collection-{job_id}")
