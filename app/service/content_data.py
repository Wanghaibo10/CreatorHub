"""作品记录的序列化与本地文件定位/删除。

从 main.py 抽出(2026-08-17 模块化):contents 与 monitors 域共用。
"""
from __future__ import annotations

from pathlib import Path

from moss.model import ContentRecord


def _content_dict(r: ContentRecord) -> dict:
    return {
        "id": r.id, "platform": r.platform, "target_id": r.target_id,
        "aweme_id": r.aweme_id, "desc": r.desc, "media_type": r.media_type,
        "quality": r.quality, "create_time": r.create_time, "cover_url": r.cover_url,
        "like_count": r.like_count, "comment_count": r.comment_count,
        "duration": r.duration, "retry_count": r.retry_count,
        "download_status": r.download_status, "local_path": r.local_path, "error": r.error,
    }


def _content_local_path(rec: ContentRecord) -> Path | None:
    if not rec.local_path:
        return None
    try:
        path = Path(rec.local_path).expanduser().resolve(strict=True)
        return path if path.is_file() or path.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _content_local_media_path(rec: ContentRecord) -> Path | None:
    """Return the downloaded single-file media for a content record, if usable."""
    path = _content_local_path(rec)
    try:
        return path if path and path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _delete_content_files(rec: ContentRecord):
    """只删除该作品自己的文件(按 aweme_id 前缀),不动作者文件夹其它内容。"""
    if not rec.local_path:
        return 0
    p = Path(rec.local_path)
    folder = p if p.is_dir() else p.parent
    if not folder.exists():
        return 0
    n = 0
    for f in folder.glob(f"{rec.aweme_id}_*"):
        try:
            f.unlink(); n += 1
        except Exception:
            pass
    return n
