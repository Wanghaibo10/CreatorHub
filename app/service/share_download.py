"""分享链接下载的领域逻辑。

从 main.py 抽出(2026-08-17 模块化):账号 cookie 落盘、抖音原生直连下载、
历史记录读写与存量补录(_backfill_share_download_history 由 lifespan 调用)。
实际下载器在 engine/share_downloader.py。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlmodel import select

from moss.core.config import get_config
from moss.common.db import get_session
from application.engine import Downloader
from application.engine.share_downloader import ShareDownloadError, detect_platform
from moss.model import DouyinAccount, ShareDownloadRecord
from application.douyin import (DouyinClient, cookie_from_state as douyin_cookie_from_state, parse_aweme, resolve_aweme_id, safe_title)
from moss.core.settings import get_setting

cfg = get_config()


def _share_input(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "请粘贴分享链接或完整分享文案")
    if len(value) > 100_000:
        raise HTTPException(400, "分享内容过长（最多 100000 个字符）")
    return value


def _write_account_cookie_file(account_id: int | None) -> tuple[str, str, str]:
    """把 Patchright storage_state 临时转换为 yt-dlp 可读的 Netscape Cookie 文件。

    返回 (cookie_file, account_proxy, account_ua)。调用方必须在使用后删除 cookie_file。
    """
    if account_id is None:
        return "", "", ""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "下载所选账号不存在")
        state_text = acc.storage_state or acc.creator_storage_state or ""
        raw_cookie = acc.cookie or ""
        platform = acc.platform
        account_proxy = acc.proxy or ""
        account_ua = acc.ua or ""

    try:
        state = json.loads(state_text or "{}")
    except Exception:
        state = {}
    cookies = list(state.get("cookies") or [])
    if not cookies and raw_cookie:
        default_domain = {
            "xhs": ".xiaohongshu.com",
            "kuaishou": ".kuaishou.com",
            "shipinhao": ".weixin.qq.com",
        }.get(platform, ".douyin.com")
        for part in raw_cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                cookies.append({
                    "name": name, "value": value, "domain": default_domain,
                    "path": "/", "secure": True, "expires": 0,
                })
    if not cookies:
        raise HTTPException(400, "所选账号没有可复用的 Cookie 登录态")

    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n",
        prefix="creatorhub-share-", suffix=".cookies.txt", delete=False,
    )
    try:
        fh.write("# Netscape HTTP Cookie File\n")
        for cookie in cookies:
            name = str(cookie.get("name") or "").replace("\t", "").replace("\n", "")
            value = str(cookie.get("value") or "").replace("\t", "").replace("\n", "")
            domain = str(cookie.get("domain") or "").strip()
            if not name or not domain:
                continue
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            path = str(cookie.get("path") or "/").replace("\t", "")
            secure = "TRUE" if cookie.get("secure") else "FALSE"
            expires_raw = cookie.get("expires") or cookie.get("expirationDate") or 0
            try:
                expires = max(0, int(float(expires_raw)))
            except (TypeError, ValueError):
                expires = 0
            fh.write(
                f"{domain}\t{include_subdomains}\t{path}\t{secure}\t"
                f"{expires}\t{name}\t{value}\n"
            )
    finally:
        fh.close()
    return fh.name, account_proxy, account_ua


def _share_file_role(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name.endswith(".info.json"):
        return "metadata"
    if ".cover." in name:
        return "thumbnail"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
        return "media"
    if suffix in {".srt", ".vtt", ".ass", ".lrc", ".ttml"}:
        return "subtitle"
    if suffix in {
        ".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v",
        ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac",
    }:
        return "media"
    if suffix in {".json", ".description"}:
        return "metadata"
    return "other"


_SHARE_HISTORY_META_KEYS = {
    "id", "title", "description", "uploader", "uploader_id", "channel",
    "duration", "timestamp", "upload_date", "view_count", "like_count",
    "comment_count", "thumbnail", "webpage_url", "original_url",
    "extractor", "extractor_key", "ext", "format", "format_id",
    "width", "height", "platform", "media_type", "media_count",
}


def _compact_share_metadata(metadata: Any) -> dict:
    """只保留历史列表需要的字段，避免把 yt-dlp 的完整响应重复写进数据库。"""
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value for key, value in metadata.items()
        if key in _SHARE_HISTORY_META_KEYS
    }


def _save_share_download_history(
    *,
    source_url: str,
    platform: str,
    account_id: int | None,
    item: dict,
) -> int:
    metadata = _compact_share_metadata(item.get("metadata"))
    files = item.get("files") if isinstance(item.get("files"), list) else []
    media_files = [file for file in files if file.get("role") == "media"]
    status = "done" if item.get("ok") else "failed"
    record = ShareDownloadRecord(
        platform=platform or str(metadata.get("platform") or detect_platform(source_url)),
        source_url=source_url,
        account_id=account_id,
        item_id=str(metadata.get("id") or ""),
        title=str(metadata.get("title") or ""),
        author=str(metadata.get("uploader") or metadata.get("channel") or ""),
        media_type=str(metadata.get("media_type") or ""),
        media_count=int(metadata.get("media_count") or len(media_files)),
        cover_url=str(metadata.get("thumbnail") or ""),
        status=status,
        output_dir=str(item.get("output_dir") or ""),
        files_json=json.dumps(files, ensure_ascii=False, default=str),
        metadata_json=json.dumps(metadata, ensure_ascii=False, default=str),
        error=str(item.get("error") or ""),
    )
    with get_session() as s:
        s.add(record)
        s.commit()
        s.refresh(record)
        return int(record.id or 0)


def _backfill_share_download_history() -> int:
    """从已有的 ``*.info.json`` 补录旧下载，升级后历史列表不会是空的。"""
    default_root = get_setting("download_dir", cfg.engine.media_dir) or cfg.engine.media_dir
    share_root = Path(default_root).expanduser() / "share"
    if not share_root.is_dir():
        return 0

    restored = 0
    # 防止用户把超大归档目录设成下载目录时启动扫描失控。
    for info_path in list(share_root.rglob("*.info.json"))[:5000]:
        try:
            metadata_raw = json.loads(info_path.read_text(encoding="utf-8"))
            if not isinstance(metadata_raw, dict):
                continue
            metadata = _compact_share_metadata(metadata_raw)
            item_id = str(metadata.get("id") or "")
            source_url = str(
                metadata.get("webpage_url")
                or metadata.get("original_url")
                or ""
            )
            if not item_id and not source_url:
                continue

            with get_session() as s:
                query = select(ShareDownloadRecord.id)
                if item_id:
                    query = query.where(ShareDownloadRecord.item_id == item_id)
                else:
                    query = query.where(ShareDownloadRecord.source_url == source_url)
                if s.exec(query.limit(1)).first() is not None:
                    continue

            prefix = f"{item_id}_" if item_id else info_path.name[:-10]
            files = []
            for path in sorted(info_path.parent.iterdir()):
                if not path.is_file() or path.suffix.lower() in {".part", ".ytdl"}:
                    continue
                # 原生抖音目录可能含多个作品，只关联相同作品 ID 前缀的文件。
                if item_id and not path.name.startswith(prefix):
                    continue
                try:
                    relative = path.relative_to(share_root).as_posix()
                    size = path.stat().st_size
                except OSError:
                    continue
                files.append({
                    "name": path.name,
                    "path": str(path.resolve()),
                    "relative_path": relative,
                    "size": size,
                    "role": _share_file_role(path),
                })

            platform = str(metadata.get("platform") or detect_platform(source_url))
            _save_share_download_history(
                source_url=source_url,
                platform=platform,
                account_id=None,
                item={
                    "ok": True,
                    "output_dir": str(info_path.parent.resolve()),
                    "metadata": metadata,
                    "files": files,
                },
            )
            restored += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return restored


def _native_aweme_metadata(aweme, source_url: str) -> dict:
    return {
        "id": aweme.aweme_id,
        "title": aweme.desc or aweme.aweme_id,
        "description": aweme.desc,
        "uploader": aweme.author_name,
        "duration": aweme.duration,
        "timestamp": aweme.create_time,
        "like_count": aweme.like_count,
        "comment_count": aweme.comment_count,
        "thumbnail": aweme.cover,
        "webpage_url": source_url,
        "original_url": source_url,
        "extractor": "creatorhub:douyin",
        "extractor_key": "CreatorHubDouyin",
        "ext": "jpg" if aweme.media_type == "images" else "mp4",
        "format": aweme.quality_label,
        "platform": "douyin",
        "media_type": aweme.media_type,
        "media_count": len(aweme.medias),
    }


async def _douyin_native_share(
    source_url: str,
    *,
    account_id: int | None,
    output_root: Path,
    quality: str,
    should_download: bool,
    save_metadata: bool,
    save_thumbnail: bool,
    proxy: str,
    user_agent: str,
) -> dict | None:
    """用 CreatorHub 自带抖音接口兜底 yt-dlp 尚未支持的 /note/、/slides/。

    返回 None 表示它不是可解析的抖音单作品链接，应继续走通用提取器。
    """
    if account_id is None:
        return None
    aweme_id = await resolve_aweme_id(source_url, user_agent)
    if not aweme_id:
        return None

    with get_session() as s:
        account = s.get(DouyinAccount, account_id)
        if not account or account.platform != "douyin":
            return None
        state = account.storage_state or account.creator_storage_state or ""
        raw_cookie = account.cookie or ""
    cookie = douyin_cookie_from_state(state) or raw_cookie
    client = DouyinClient(cookie, user_agent,
                          timeout=cfg.engine.request_timeout_seconds,
                          proxy=proxy)
    raw = await client.fetch_video_detail(aweme_id)
    if not raw:
        raise ShareDownloadError(
            "已识别到抖音作品 ID，但所选账号未能读取作品详情；"
            "请检查账号登录态或更换抖音账号"
        )
    aweme = parse_aweme(raw, quality if quality != "audio" else "highest")
    if not aweme:
        raise ShareDownloadError("抖音作品详情已读取，但没有找到可下载的视频或图片")

    # 原生直链下载器不做音频转码；仅音频请求继续交给 yt-dlp/ffmpeg。
    if quality == "audio" and aweme.media_type == "video":
        return None

    metadata = _native_aweme_metadata(aweme, source_url)
    if not should_download:
        return {
            "ok": True,
            "url": source_url,
            "metadata": metadata,
            "warnings": [],
        }

    downloader = Downloader(
        str(output_root),
        user_agent,
        timeout=max(30.0, cfg.engine.request_timeout_seconds),
    )
    ok, _local_path, error = await downloader.download_aweme(
        aweme, base_dir=str(output_root), proxy=proxy
    )
    if not ok:
        raise ShareDownloadError(error or "抖音媒体下载失败")

    target_dir = output_root / safe_title(aweme.author_name or "unknown")
    title = safe_title(aweme.desc) or aweme.aweme_id
    if save_metadata:
        info_path = target_dir / f"{aweme.aweme_id}_{title}.info.json"
        payload = {
            **metadata,
            "media": [
                {"url": media.url, "kind": media.kind, "ext": media.ext,
                 "index": media.index}
                for media in aweme.medias
            ],
            "raw": raw,
        }
        info_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # 视频封面单独保存；图文作品的图片本身已经全部下载。
    if save_thumbnail and aweme.media_type == "video" and aweme.cover:
        import httpx
        from application.browser.manager import normalize_proxy

        cover_path = target_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
        headers = {"User-Agent": user_agent, "Referer": "https://www.douyin.com/"}
        try:
            async with httpx.AsyncClient(
                timeout=max(30.0, cfg.engine.request_timeout_seconds),
                follow_redirects=True,
                headers=headers,
                proxy=normalize_proxy(proxy) or None,
            ) as http:
                await downloader._download_one(http, aweme.cover, cover_path)
        except Exception:
            pass

    files = []
    for path in sorted(target_dir.glob(f"{aweme.aweme_id}_*")):
        if not path.is_file() or path.suffix.lower() in {".part", ".ytdl"}:
            continue
        files.append({
            "name": path.name,
            "path": str(path.resolve()),
            "relative_path": path.relative_to(output_root).as_posix(),
            "size": path.stat().st_size,
            "role": _share_file_role(path),
        })
    if not any(item["role"] == "media" for item in files):
        raise ShareDownloadError("抖音作品解析成功，但本地没有生成媒体文件")
    return {
        "ok": True,
        "job_id": f"douyin_{aweme.aweme_id}",
        "url": source_url,
        "output_dir": str(target_dir.resolve()),
        "metadata": metadata,
        "files": files,
        "progress": {"status": "finished"},
        "warnings": [],
    }


def _share_history_files(record: ShareDownloadRecord) -> list[dict]:
    try:
        files = json.loads(record.files_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        files = []
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


def _share_history_local_path(
    record: ShareDownloadRecord,
    media_index: int | None = None,
) -> Path | None:
    files = _share_history_files(record)
    candidates = [item for item in files if item.get("role") == "media"]
    if media_index is not None:
        if media_index < 0 or media_index >= len(candidates):
            return None
        candidates = [candidates[media_index]]
    if not candidates:
        candidates = files
    for item in candidates:
        raw = str(item.get("path") or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if path.is_file() or path.is_dir():
            return path
    if media_index is None and record.output_dir:
        try:
            path = Path(record.output_dir).expanduser().resolve(strict=True)
            if path.is_dir():
                return path
        except (OSError, RuntimeError):
            pass
    return None
