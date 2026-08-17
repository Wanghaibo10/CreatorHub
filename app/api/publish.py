"""发布任务 API(创作平台发布 + 跨平台转发 + 已发列表)。

从 main.py 抽出(2026-08-17 模块化)。任务执行在 engine/monitor.py,
读操作统一走 services/account_ops.py 的风控闸门。
"""
from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import select

from moss.core.config import get_config
from moss.common.db import get_session
from moss.model import ContentRecord, DouyinAccount, PublishTask
from application.registry import ARTICLE_KEYS, label as platform_label
from application.xhs import has_creator_cookies
from moss.core.risk import OperationKind, RiskCategory, classify_platform_error
from moss.core.runtime import rt
from app.service.account_ops import _run_account_read

router = APIRouter(tags=["publish"])
cfg = get_config()


UPLOAD_DIR = Path("./data/uploads")


@router.post("/api/publish/upload")
async def publish_upload(files: list[UploadFile] = File(...)):
    """上传图集/视频文件,返回本地路径列表(供创建发布任务用)。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        ext = Path(f.filename or "").suffix or ".bin"
        name = f"{_uuid.uuid4().hex}{ext}"
        dest = UPLOAD_DIR / name
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        saved.append({"path": str(dest), "name": f.filename})
    return {"files": saved}


class PublishIn(BaseModel):
    account_id: int
    media_type: str = "images"            # images | video
    title: str = ""
    desc: str = ""
    topics: str = ""
    location: str = ""                    # 视频号:位置 POI(可选)
    media_paths: list[str] = []
    visibility: str = "public"            # 抖音:public | friends | private
    allow_save: bool = True               # 抖音:是否允许他人保存
    scheduled_at: str | None = None       # ISO 时间(本地),空=尽快发


class PublishUpdate(BaseModel):
    account_id: int | None = None
    title: str | None = None
    desc: str | None = None
    topics: str | None = None
    location: str | None = None
    visibility: str | None = None
    allow_save: bool | None = None
    scheduled_at: str | None = None


def _publish_dict(t: PublishTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "account_id": t.account_id,
        "media_type": t.media_type, "title": t.title, "desc": t.desc,
        "topics": t.topics, "location": t.location,
        "status": t.status, "result_url": t.result_url,
        "visibility": t.visibility, "allow_save": t.allow_save,
        "error": t.error, "media_count": len(json.loads(t.media_json or "[]")),
        "source_platform": t.source_platform, "source_content_id": t.source_content_id,
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _parse_when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None


@router.get("/api/publish")
async def list_publish(platform: str | None = None):
    with get_session() as s:
        q = select(PublishTask)
        if platform:
            q = q.where(PublishTask.platform == platform)
        rows = s.exec(q.order_by(PublishTask.id.desc())).all()
        return [_publish_dict(t) for t in rows]


@router.post("/api/publish")
async def add_publish(body: PublishIn):
    if body.media_type not in ("images", "video"):
        raise HTTPException(400, "media_type 须为 images 或 video")
    paths = [p for p in body.media_paths if Path(p).exists()]
    if not paths:
        raise HTTPException(400, "没有可用的媒体文件,请先上传")
    with get_session() as s:
        acc = s.get(DouyinAccount, body.account_id)
        if not acc:
            raise HTTPException(400, "请选择一个已登录的账号")
        pname = platform_label(acc.platform)
        if acc.platform in ARTICLE_KEYS:
            # 图文平台(百家号/头条/公众号):纯协议发布,凭证是账号 Cookie。
            # 微博是纯登录态管理平台(publish_via="none"),不接发布
            from application.registry import spec as _spec
            if _spec(acc.platform).publish_via == "none":
                raise HTTPException(400, f"{pname} 仅做登录态管理,不支持发布")
            if not acc.cookie:
                raise HTTPException(400, f"该{pname}账号没有 Cookie,请先在账号页登录")
        elif acc.platform in ("kuaishou", "douyin", "shipinhao"):
            # 抖音 / 快手 / 视频号发布走浏览器自动化,登录态在该账号持久 profile 里
            if not (acc.creator_storage_state or acc.storage_state):
                raise HTTPException(400, f"该{pname}账号不可发布:请先在账号页完成登录")
        elif acc.platform == "xhs":
            if not (acc.creator_storage_state or has_creator_cookies(acc.storage_state)):
                raise HTTPException(400, "该账号不可发布:请对该号完成「小红书扫码登录」或「创作者登录」")
        else:
            raise HTTPException(400, f"{pname} 不支持发布")
        vis = body.visibility if body.visibility in ("public", "friends", "private") else "public"
        # 标题上限:图文平台 64 字(百家号/公众号),视频平台 20 字
        title_limit = 64 if acc.platform in ARTICLE_KEYS else 20
        t = PublishTask(
            platform=acc.platform, account_id=body.account_id, media_type=body.media_type,
            title=body.title.strip()[:title_limit], desc=body.desc, topics=body.topics,
            location=(body.location or "").strip()[:60],
            visibility=vis, allow_save=bool(body.allow_save),
            media_json=json.dumps(paths), scheduled_at=_parse_when(body.scheduled_at),
        )
        s.add(t); s.commit(); s.refresh(t)
        return _publish_dict(t)


@router.put("/api/publish/{tid}")
async def update_publish(tid: int, body: PublishUpdate):
    with get_session() as s:
        t = s.get(PublishTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status not in ("pending", "failed", "canceled"):
            raise HTTPException(400, f"任务状态为 {t.status},不可编辑")
        if body.account_id is not None:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != t.platform or acc.status != "active":
                raise HTTPException(400, "发布账号不存在、登录态失效或与任务平台不匹配")
            t.account_id = body.account_id
        if body.title is not None:
            t.title = body.title.strip()[:64 if t.platform in ARTICLE_KEYS else 20]
        if body.desc is not None:
            t.desc = body.desc
        if body.topics is not None:
            t.topics = body.topics.strip()
        if body.location is not None:
            t.location = body.location.strip()[:60]
        if body.visibility is not None:
            if body.visibility not in ("public", "friends", "private"):
                raise HTTPException(400, "可见范围须为 public、friends 或 private")
            t.visibility = body.visibility
        if body.allow_save is not None:
            t.allow_save = body.allow_save
        if "scheduled_at" in body.model_fields_set:
            if body.scheduled_at and _parse_when(body.scheduled_at) is None:
                raise HTTPException(400, "定时发布时间格式无效")
            t.scheduled_at = _parse_when(body.scheduled_at)
        if t.status in ("failed", "canceled"):
            t.status = "pending"
            t.error = ""
        s.add(t); s.commit(); s.refresh(t)
        return _publish_dict(t)


@router.post("/api/publish/{tid}/run-now")
async def run_publish(tid: int):
    if not rt.engine:
        raise HTTPException(503, "引擎未就绪")
    return await rt.engine.publish_task(tid)


@router.delete("/api/publish/{tid}")
async def del_publish(tid: int):
    with get_session() as s:
        t = s.get(PublishTask, tid)
        if t:
            s.delete(t); s.commit()
    return {"ok": True}


def _first_val(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0, []):
            return v
    return default


async def _xhs_account_uid(state: str, proxy: str = "", *,
                           detailed: bool = False):
    """拿到该账号自己的 user_id(self_info → 创作平台资料兜底)。"""
    from application.xhs import XhsApiClient, cookie_str_from_state, has_a1, creator_profile
    cookie = cookie_str_from_state(state)
    if has_a1(cookie):
        try:
            client = XhsApiClient(cookie, cfg.engine.user_agent,
                                  timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
            me = await client.self_info()
            uid = str((me or {}).get("user_id") or "")
            if uid:
                return (uid, "") if detailed else uid
        except Exception as exc:
            category, _signal = classify_platform_error(exc)
            if detailed and category in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                return "", exc
    try:
        if detailed:
            prof, profile_error = await creator_profile(
                state, proxy=proxy, preserve_error=True)
        else:
            prof = await creator_profile(state, proxy=proxy)
            profile_error = ""
    except Exception as exc:
        if detailed:
            return "", exc
        raise
    if detailed and profile_error:
        return "", profile_error
    uid = (prof or {}).get("sec_uid") or ""
    return (uid, "") if detailed else uid


def _imgs_of(n: dict) -> list:
    out = []
    for it in (n.get("images_list") or n.get("imageList") or []):
        if isinstance(it, dict):
            u = it.get("url") or it.get("url_default") or it.get("urlDefault") or ""
            if u:
                out.append(u)
    return out


@router.get("/api/publish/published")
async def list_published_notes(account_id: int):
    """拉取「已发布作品列表」。
    优先用「读取登录态」打开自己的 www 主页(token 对预览/评论有效);
    没有读取态时回退创作平台「笔记管理」(能显示,但视频预览/评论可能不可用)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform != "xhs":
            raise HTTPException(400, "请选择一个已登录的小红书账号")
        read_state = acc.storage_state or ""
        creator_state = acc.creator_storage_state or ""
        proxy = acc.proxy or ""
        if not (read_state or creator_state):
            raise HTTPException(400, "该账号未登录,请先在账号页扫码登录")
    from application.browser import fetch_xhs_notes, fetch_creator_published
    from application.xhs import parse_note_brief

    async def _fetch_published_notes():
        def _payload(items, good_tokens=False, error_category=""):
            payload = {
                "notes": items,
                "total": len(items),
                "good_tokens": good_tokens,
            }
            if error_category:
                payload["_error_category"] = error_category
            return payload

        def _failure(error):
            category, _signal = classify_platform_error(error)
            return _payload([], error_category=category.value), error

        with get_session() as s:
            current = s.get(DouyinAccount, account_id)
            identity = rt.browser.identity_for(current)
        out, good = [], False
        if read_state:
            uid, uid_error = await _xhs_account_uid(
                read_state, proxy, detailed=True)
            if uid_error:
                category, _signal = classify_platform_error(uid_error)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    return _failure(uid_error)
            if uid:
                try:
                    items, _a, read_error = await fetch_xhs_notes(
                        rt.browser, identity, uid, set())
                except Exception as exc:
                    category, _signal = classify_platform_error(exc)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        return _failure(exc)
                    items, read_error = [], exc
                if read_error:
                    category, _signal = classify_platform_error(read_error)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        return _failure(read_error)
                for raw in items[:80]:
                    b = parse_note_brief(raw)
                    if not b:
                        continue
                    card = raw.get("note_card") or raw
                    interact = card.get("interact_info") or {}
                    out.append({
                        "note_id": b["note_id"],
                        "title": b.get("title") or "(无标题)",
                        "type": b.get("type") or "normal",
                        "cover": b.get("cover") or "",
                        "images": [],
                        "like": interact.get("liked_count") or 0,
                        "time": card.get("time") or 0,
                        "xsec_token": b.get("xsec_token") or "",
                        "xsec_source": "pc_feed",
                    })
                good = bool(out)
        error = ""
        if not out:   # 回退:创作平台笔记管理(显示用)
            try:
                notes, error = await fetch_creator_published(rt.browser, identity)
            except Exception as exc:
                category, _signal = classify_platform_error(exc)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    return _failure(exc)
                raise
            for n in notes[:80]:
                imgs = _imgs_of(n)
                vi = n.get("video_info") or {}
                cover = (imgs[0] if imgs else
                         (vi.get("cover") if isinstance(vi, dict) else ""))
                out.append({
                    "note_id": str(_first_val(n, "id", "noteId", "note_id")),
                    "title": _first_val(
                        n, "display_title", "title", "desc", default="(无标题)"),
                    "type": _first_val(n, "type", "noteType", default="normal"),
                    "cover": cover or "", "images": imgs,
                    "like": _first_val(n, "likes", "likeCount", default=0),
                    "time": _first_val(n, "time", "postTime", default=0),
                    "xsec_token": _first_val(n, "xsec_token", default=""),
                    "xsec_source": _first_val(
                        n, "xsec_source", default="pc_note_detail"),
                })
        if error:
            category, _signal = classify_platform_error(error)
            return _payload(
                out, good, error_category=category.value), error
        return _payload(out, good), ""

    payload, outcome = await _run_account_read(
        account_id, OperationKind.READ_LIGHT, f"published:{account_id}",
        _fetch_published_notes,
        empty_result={"notes": [], "total": 0, "good_tokens": False},
        unexpected_detail="读取已发布作品失败")
    if isinstance(outcome, dict):
        return outcome
    error_category = payload.pop("_error_category", "")
    if error_category == RiskCategory.AUTH.value or "logged_out" in (outcome or ""):
        raise HTTPException(400, "登录态已失效,请对该账号点「重新登录」")
    if error_category in {
            RiskCategory.RISK.value, RiskCategory.NETWORK.value}:
        raise HTTPException(400, f"读取已发布作品失败:{outcome}")
    return payload


@router.get("/api/publish/note-media")
async def publish_note_media(account_id: int, note_id: str,
                             xsec_token: str = "", xsec_source: str = "pc_note_detail"):
    """取一条小红书笔记的完整媒体(图集/视频),供「已发布作品」预览。"""
    from application.xhs import (XhsApiClient, XhsApiError, cookie_str_from_state, has_a1, parse_note_detail)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform != "xhs":
            raise HTTPException(400, "账号无效")
        state = acc.storage_state or acc.creator_storage_state or ""
        proxy = acc.proxy or ""
    cookie = cookie_str_from_state(state)
    if not has_a1(cookie):
        raise HTTPException(400, "登录态缺少 a1")

    no_media = "拿不到该笔记的媒体(xsec_token 对 feed 接口无效)"

    async def _fetch_note_media():
        client = XhsApiClient(
            cookie, cfg.engine.user_agent,
            timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
        try:
            card = await client.note_detail(
                note_id, xsec_token=xsec_token, xsec_source=xsec_source)
        except XhsApiError as exc:
            return None, exc
        aw = parse_note_detail(card or {}, {"note_id": note_id})
        if not aw or not aw.medias:
            return None, no_media
        return {
            "media_type": aw.media_type,
            "desc": aw.desc,
            "cover_url": aw.cover or "",
            "medias": [{
                "url": media.url,
                "kind": media.kind,
                "ext": media.ext,
                "index": media.index,
            } for media in aw.medias],
        }, ""

    payload, outcome = await _run_account_read(
        account_id, OperationKind.READ_HEAVY,
        f"note-media:{account_id}:{note_id}", _fetch_note_media,
        empty_result={
            "media_type": "", "desc": "", "cover_url": "", "medias": []},
        unexpected_detail="取笔记失败")
    if isinstance(outcome, dict):
        return outcome
    if outcome == no_media:
        raise HTTPException(400, no_media)
    if outcome:
        raise HTTPException(400, f"取笔记失败:{outcome}")
    return payload


@router.get("/api/publish/note-comments")
async def publish_note_comments(account_id: int, note_id: str,
                                xsec_token: str = "", xsec_source: str = "pc_note_detail"):
    """拉取一条小红书笔记的评论(一级 + 子评论拍平)。"""
    from application.xhs import (XhsApiClient, XhsApiError, cookie_str_from_state, has_a1, parse_comment as parse_xhs_comment, flatten_comments)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc or acc.platform != "xhs":
            raise HTTPException(400, "账号无效")
        state = acc.storage_state or acc.creator_storage_state or ""
        proxy = acc.proxy or ""
    cookie = cookie_str_from_state(state)
    if not has_a1(cookie):
        raise HTTPException(400, "登录态缺少 a1")

    async def _fetch_note_comments():
        client = XhsApiClient(
            cookie, cfg.engine.user_agent,
            timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
        # 评论接口要 pc_feed 令牌;先调 feed 拿一个新鲜令牌(feed 接受 pc_creatormng 令牌)
        tok, src = xsec_token, xsec_source
        try:
            item = await client.note_detail_raw(
                note_id, xsec_token=xsec_token, xsec_source=xsec_source)
            fresh_token = (item.get("xsec_token") or
                           ((item.get("note_card") or {}).get("xsec_token")))
            if fresh_token:
                tok, src = fresh_token, "pc_feed"
        except XhsApiError as exc:
            if exc.category in {"risk", "auth", "network"}:
                return None, exc
        except Exception as exc:
            category, _signal = classify_platform_error(exc)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                return None, exc
            raise
        try:
            data = await client.note_comments(
                note_id, xsec_token=tok, xsec_source=src)
        except XhsApiError as exc:
            return None, exc
        raw = data.get("comments") or []
        comments = [
            comment for comment in (
                parse_xhs_comment(item) for item in flatten_comments(raw))
            if comment
        ]
        comments.sort(
            key=lambda comment: comment.get("create_time") or 0, reverse=True)
        return {
            "comments": comments,
            "total": len(comments),
            "has_more": bool(data.get("has_more")),
        }, ""

    payload, outcome = await _run_account_read(
        account_id, OperationKind.READ_HEAVY,
        f"note-comments:{account_id}:{note_id}", _fetch_note_comments,
        empty_result={"comments": [], "total": 0, "has_more": False},
        unexpected_detail="取评论失败")
    if isinstance(outcome, dict):
        return outcome
    if outcome:
        raise HTTPException(400, f"取评论失败:{outcome}")
    return payload


class RepostIn(BaseModel):
    account_id: int
    scheduled_at: str | None = None
    # 转发前可编辑的笔记信息;为 None 时沿用作品原始内容
    title: str | None = None
    desc: str | None = None
    topics: str | None = None
    visibility: str = "public"           # 抖音:public | friends | private
    allow_save: bool = True              # 抖音:是否允许他人保存
    media_order: list[int] | None = None  # 剔除/调序后保留的图片原始序号(按新顺序);None=全部原序


async def _repost_content(cid: int, body: RepostIn, target_platform: str):
    """把已下载作品转成目标平台(xhs / douyin / shipinhao)的发布任务。"""
    if not rt.engine:
        raise HTTPException(503, "引擎未就绪")
    # 1) 只在会话内做校验,取出需要的值后退出会话,不把 ORM 对象带出去
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "作品不存在")
        if rec.download_status != "done":
            raise HTTPException(400, "该作品尚未下载完成,无法转发")
        acc = s.get(DouyinAccount, body.account_id)
        if not acc or acc.platform != target_platform:
            pname = {"douyin": "抖音", "shipinhao": "视频号"}.get(
                target_platform, "小红书")
            raise HTTPException(400, f"请选择一个已登录的{pname}账号")
        if target_platform in ("douyin", "shipinhao"):
            # 抖音/视频号发布走浏览器自动化，有任一持久登录态即可。
            if not (acc.creator_storage_state or acc.storage_state):
                pname = "视频号" if target_platform == "shipinhao" else "抖音"
                action = "视频号登录" if target_platform == "shipinhao" else "创作者登录"
                raise HTTPException(400, f"该{pname}账号不可发布:请先在账号页完成「{action}」")
        elif not (acc.creator_storage_state or has_creator_cookies(acc.storage_state)):
            raise HTTPException(400, "该账号不可发布:请对该号完成「小红书扫码登录」或「创作者登录」")
    # 2) 退出会话后再创建发布任务(create_relay_publish 内部自开会话)
    #    若前端传了编辑后的标题/正文/话题,则用编辑值覆盖作品原始内容
    vis = body.visibility if body.visibility in ("public", "friends", "private") else "public"
    tid = rt.engine.create_relay_publish(
        cid, body.account_id, target_platform=target_platform,
        title=body.title, desc=body.desc, topics=body.topics,
        visibility=vis, allow_save=bool(body.allow_save),
        media_order=body.media_order)
    if not tid:
        raise HTTPException(400, "未找到该作品的本地文件,无法转发")
    # 3) 定时时间另开一个会话更新
    if body.scheduled_at:
        with get_session() as s:
            t = s.get(PublishTask, tid)
            if t:
                t.scheduled_at = _parse_when(body.scheduled_at)
                s.add(t); s.commit()
    return {"ok": True, "task_id": tid}


@router.post("/api/contents/{cid}/repost-xhs")
async def repost_to_xhs(cid: int, body: RepostIn):
    """把一条已下载的抖音作品转成小红书发布任务。"""
    return await _repost_content(cid, body, "xhs")


@router.post("/api/contents/{cid}/repost-douyin")
async def repost_to_douyin(cid: int, body: RepostIn):
    """把一条已下载的小红书作品转成抖音发布任务(反向转发)。"""
    return await _repost_content(cid, body, "douyin")


@router.post("/api/contents/{cid}/repost-shipinhao")
async def repost_to_channels(cid: int, body: RepostIn):
    """把一条已下载的抖音作品转成视频号发布任务。"""
    return await _repost_content(cid, body, "shipinhao")
