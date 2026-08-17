"""链接下载历史的序列化辅助。

从 main.py 抽出(2026-08-17 模块化):share-download 域与 reports 导出共用。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from moss.model import ShareDownloadRecord


def _share_history_dict(record: ShareDownloadRecord) -> dict:
    try:
        files = json.loads(record.files_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        files = []
    if not isinstance(files, list):
        files = []
    try:
        metadata = json.loads(record.metadata_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    media_files = [item for item in files if isinstance(item, dict) and item.get("role") == "media"]
    first_file = media_files[0] if media_files else (files[0] if files and isinstance(files[0], dict) else {})

    def number(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    description = str(record.title or metadata.get("title") or metadata.get("description") or "")
    create_time = number(metadata.get("timestamp"))
    if not create_time:
        upload_date = str(metadata.get("upload_date") or "")
        if len(upload_date) == 8 and upload_date.isdigit():
            try:
                create_time = int(datetime.strptime(upload_date, "%Y%m%d").timestamp())
            except ValueError:
                create_time = 0
    local_path = str(first_file.get("path") or "") if isinstance(first_file, dict) else ""
    quality = str(metadata.get("format") or metadata.get("format_id") or "")
    return {
        "id": record.id,
        "platform": record.platform,
        "source_url": record.source_url,
        "account_id": record.account_id,
        "item_id": record.item_id,
        "title": record.title,
        "author": record.author,
        "media_type": record.media_type,
        "media_count": record.media_count,
        "cover_url": record.cover_url,
        "status": record.status,
        "output_dir": record.output_dir,
        "files": files if isinstance(files, list) else [],
        "metadata": metadata if isinstance(metadata, dict) else {},
        "error": record.error,
        "created_at": record.created_at.isoformat() if record.created_at else "",
        # 与「作品监控」列表兼容的展示字段，方便链接下载历史复用同一套作品表格。
        "aweme_id": record.item_id,
        "desc": description,
        "create_time": create_time,
        "quality": quality,
        "like_count": number(metadata.get("like_count")),
        "comment_count": number(metadata.get("comment_count")),
        "duration": number(metadata.get("duration")),
        "download_status": record.status,
        "local_path": local_path,
    }
