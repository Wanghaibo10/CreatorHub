"""监控目标 API(/api/monitors)。

从 main.py 抽出(2026-08-17 模块化)。扫描执行在 engine/monitor.py,
作品序列化在 services/content_data.py。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from moss.core.config import get_config
from moss.common.db import get_session
from moss.model import ContentRecord, DouyinAccount, MonitorTarget
from application.douyin import resolve_sec_uid
from application.kuaishou import resolve_ks_user_id
from application.xhs import resolve_user as xhs_resolve_user
from moss.core.runtime import rt
from app.service.content_data import _content_dict
from app.service.monitor_meta import _dump_meta_tags, _load_meta_tags, _meta_tags, _meta_text
from moss.core.settings import QUALITY_CHOICES

router = APIRouter(tags=["monitors"])
cfg = get_config()

class TargetIn(BaseModel):
    url_or_secuid: str                       # 抖音/小红书主页链接 或 小红书关键词
    platform: str = "douyin"                # douyin | xhs
    target_kind: str = "creator"            # creator | keyword(仅小红书)
    account_id: int | None = None
    interval_seconds: int = 300
    initial_backfill_count: int | None = None
    download_dir: str = ""
    video_quality: str = ""
    download_enabled: bool = True
    media_filter: str = "all"
    alias: str = ""
    group_name: str = ""
    tags: list[str] = PydanticField(default_factory=list)


class TargetUpdate(BaseModel):
    download_dir: str | None = None
    interval_seconds: int | None = None
    initial_backfill_count: int | None = None
    video_quality: str | None = None
    download_enabled: bool | None = None
    media_filter: str | None = None
    account_id: int | None = None
    alias: str | None = None
    group_name: str | None = None
    tags: list[str] | None = None


@router.post("/api/monitors")
async def add_monitor(body: TargetIn):
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou") else "douyin"
    sec_uid = keyword = xsec_token = ""
    kind = "creator"

    if platform == "xhs" and body.target_kind == "keyword":
        kind = "keyword"
        keyword = body.url_or_secuid.strip()
        if not keyword:
            raise HTTPException(400, "请输入要监控的搜索关键词")
    elif platform == "xhs":
        ref = await xhs_resolve_user(body.url_or_secuid, cfg.engine.user_agent)
        if not ref:
            raise HTTPException(400, "无法解析小红书 user_id,请粘贴创作者主页链接 / xhslink 短链 / 24 位 user_id")
        sec_uid, xsec_token = ref.user_id, ref.xsec_token
    elif platform == "kuaishou":
        sec_uid = await resolve_ks_user_id(body.url_or_secuid, cfg.engine.user_agent)
        if not sec_uid:
            raise HTTPException(400, "无法解析快手 user_id,请粘贴创作者主页链接 / v.kuaishou.com 短链 / user_id")
    else:
        sec_uid = await resolve_sec_uid(body.url_or_secuid, cfg.engine.user_agent)
        if not sec_uid:
            raise HTTPException(400, "无法解析 sec_uid,请粘贴主页链接 / v.douyin.com 短链 / sec_uid")

    dl = body.download_dir.strip()
    if not 60 <= body.interval_seconds <= 86400:
        raise HTTPException(400, "监控间隔须为 60~86400 秒")
    if body.media_filter not in ("all", "video", "images"):
        raise HTTPException(400, "媒体筛选须为 all、video 或 images")
    if dl:
        try:
            Path(dl).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"下载目录不可用: {e}")
    with get_session() as s:
        if platform == "douyin":
            if not body.account_id:
                raise HTTPException(
                    400, "抖音作品监控必须选择已登录账号,匿名抓取可能返回陈旧或残缺作品")
            monitor_acc = s.get(DouyinAccount, body.account_id)
            if (not monitor_acc or monitor_acc.platform != "douyin"
                    or monitor_acc.status != "active"):
                raise HTTPException(400, "所选抖音账号不存在或登录态已失效")
        elif body.account_id:
            monitor_acc = s.get(DouyinAccount, body.account_id)
            if not monitor_acc or monitor_acc.platform != platform:
                raise HTTPException(400, "所选账号不存在或与监控平台不匹配")
        if kind == "keyword":
            dup = s.exec(select(MonitorTarget).where(MonitorTarget.platform == platform)
                         .where(MonitorTarget.keyword == keyword)).first()
        else:
            dup = s.exec(select(MonitorTarget).where(MonitorTarget.platform == platform)
                         .where(MonitorTarget.sec_uid == sec_uid)).first()
        if dup:
            raise HTTPException(409, "该监控目标已存在")
        q = body.video_quality.strip()
        if q and q not in QUALITY_CHOICES:
            raise HTTPException(400, f"画质取值无效: {q}")
        backfill_count = (cfg.engine.monitor_initial_backfill_count
                          if body.initial_backfill_count is None
                          else body.initial_backfill_count)
        if backfill_count < -1 or backfill_count > 1000:
            raise HTTPException(400, "首次回填数须为 -1(尽可能全量)或 0~1000")
        t = MonitorTarget(platform=platform, target_kind=kind, keyword=keyword,
                          sec_uid=sec_uid, xsec_token=xsec_token,
                          nickname=("#" + keyword) if kind == "keyword" else "",
                          alias=_meta_text(body.alias, 60),
                          group_name=_meta_text(body.group_name, 40),
                          tags=_dump_meta_tags(_meta_tags(body.tags)),
                          account_id=body.account_id,
                          interval_seconds=body.interval_seconds, download_dir=dl,
                          initial_backfill_count=backfill_count, video_quality=q,
                          download_enabled=body.download_enabled,
                          media_filter=body.media_filter)
        s.add(t); s.commit(); s.refresh(t)
        return _target_dict(t)


@router.put("/api/monitors/{tid}")
async def update_monitor(tid: int, body: TargetUpdate):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if not t:
            raise HTTPException(404)
        if body.download_dir is not None:
            dl = body.download_dir.strip()
            if dl:
                try:
                    Path(dl).expanduser().mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    raise HTTPException(400, f"下载目录不可用: {e}")
            t.download_dir = dl
        if body.interval_seconds is not None:
            if not 60 <= body.interval_seconds <= 86400:
                raise HTTPException(400, "监控间隔须为 60~86400 秒")
            t.interval_seconds = body.interval_seconds
        if body.initial_backfill_count is not None:
            if t.last_scan_at is not None:
                raise HTTPException(400, "首次历史回填仅能在第一次扫描前修改")
            if body.initial_backfill_count < -1 or body.initial_backfill_count > 1000:
                raise HTTPException(400, "首次回填数须为 -1 或 0~1000")
            t.initial_backfill_count = body.initial_backfill_count
        if body.video_quality is not None:
            q = body.video_quality.strip()
            if q and q not in QUALITY_CHOICES:
                raise HTTPException(400, f"画质取值无效: {q}")
            t.video_quality = q
        if body.download_enabled is not None:
            t.download_enabled = body.download_enabled
        if body.media_filter is not None:
            if body.media_filter not in ("all", "video", "images"):
                raise HTTPException(400, "媒体筛选须为 all、video 或 images")
            t.media_filter = body.media_filter
        if body.account_id is not None:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != t.platform or acc.status != "active":
                raise HTTPException(400, "账号不存在、登录态失效或与监控平台不匹配")
            t.account_id = body.account_id
        if body.alias is not None:
            t.alias = _meta_text(body.alias, 60)
        if body.group_name is not None:
            t.group_name = _meta_text(body.group_name, 40)
        if body.tags is not None:
            t.tags = _dump_meta_tags(_meta_tags(body.tags))
        s.add(t); s.commit(); s.refresh(t)
        return _target_dict(t)


@router.get("/api/monitors")
async def list_monitors(platform: str | None = None):
    with get_session() as s:
        q = select(MonitorTarget)
        if platform:
            q = q.where(MonitorTarget.platform == platform)
        ts = s.exec(q).all()
        out = []
        for t in ts:
            d = _target_dict(t)
            d["content_count"] = len(s.exec(
                select(ContentRecord).where(ContentRecord.target_id == t.id)).all())
            out.append(d)
        return out


@router.post("/api/monitors/{tid}/toggle")
async def toggle_monitor(tid: int):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if not t:
            raise HTTPException(404)
        t.enabled = not t.enabled
        s.add(t); s.commit()
        return {"enabled": t.enabled}


@router.post("/api/monitors/{tid}/run-now")
async def run_now(tid: int):
    if not rt.engine:
        raise HTTPException(503, "引擎未就绪")
    return await rt.engine.scan_target(tid)


@router.delete("/api/monitors/{tid}")
async def del_monitor(tid: int):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if t:
            s.delete(t); s.commit()
    return {"ok": True}


@router.get("/api/monitors/{tid}/contents")
async def target_contents(tid: int):
    with get_session() as s:
        rows = s.exec(select(ContentRecord)
                      .where(ContentRecord.target_id == tid)
                      .order_by(ContentRecord.create_time.desc())).all()
        return [_content_dict(r) for r in rows]


def _target_dict(t: MonitorTarget) -> dict:
    return {
        "id": t.id, "platform": t.platform, "target_kind": t.target_kind,
        "keyword": t.keyword,
        "sec_uid": t.sec_uid, "nickname": t.nickname, "avatar": t.avatar,
        "alias": t.alias, "group_name": t.group_name,
        "tags": _load_meta_tags(t.tags),
        "enabled": t.enabled, "interval_seconds": t.interval_seconds,
        "initial_backfill_count": t.initial_backfill_count,
        "download_dir": t.download_dir, "video_quality": t.video_quality,
        "download_enabled": t.download_enabled, "media_filter": t.media_filter,
        "account_id": t.account_id,
        "last_scan_at": t.last_scan_at.isoformat() if t.last_scan_at else None,
        "last_error": t.last_error,
    }
