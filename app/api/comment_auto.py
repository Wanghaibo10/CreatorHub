"""自动评论 API(规则 + 任务)。

从 main.py 抽出(2026-08-17 模块化)。规则的实际执行在 engine/monitor.py
(run_comment_rule / execute_comment_task),这里只做规则与任务的增删改查、
目标解析和「立即执行」入口。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from moss.core.config import get_config
from moss.common.db import get_session
from moss.model import CommentRule, CommentTask, DouyinAccount
from application.douyin import resolve_aweme_id, resolve_sec_uid
from application.kuaishou import resolve_ks_photo_id, resolve_ks_user_id
from application.xhs import resolve_note as xhs_resolve_note
from application.xhs import resolve_user as xhs_resolve_user
from moss.core.runtime import rt

router = APIRouter(tags=["comment-auto"])
cfg = get_config()

class CommentRuleIn(BaseModel):
    platform: str = "douyin"
    name: str = ""
    mode: str = "auto_reply"            # auto_reply | auto_comment
    account_id: int
    target_kind: str = "self"          # reply: self|work ; comment: keyword|creator
    target: str = ""                   # 关键词,或 创作者/作品 的链接/id
    templates: list[str] = []
    use_ai: bool = False
    require_review: bool = False
    reply_filter: str = ""
    skip_keywords: str = ""
    daily_cap: int = 20
    min_gap_seconds: int = 90
    max_per_run: int = 5
    interval_seconds: int = 1800
    enabled: bool = False


class CommentRuleUpdate(BaseModel):
    name: str | None = None
    templates: list[str] | None = None
    use_ai: bool | None = None
    require_review: bool | None = None
    reply_filter: str | None = None
    skip_keywords: str | None = None
    daily_cap: int | None = None
    min_gap_seconds: int | None = None
    max_per_run: int | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    # 改目标(任一非空则重新解析)。account_id 可单独改。
    account_id: int | None = None
    mode: str | None = None
    target_kind: str | None = None
    target: str | None = None


def _rule_dict(r: CommentRule) -> dict:
    return {
        "id": r.id, "platform": r.platform, "name": r.name, "mode": r.mode,
        "account_id": r.account_id, "target_kind": r.target_kind,
        "keyword": r.keyword, "sec_uid": r.sec_uid, "aweme_id": r.aweme_id,
        "templates": json.loads(r.templates or "[]"), "use_ai": r.use_ai,
        "require_review": r.require_review,
        "reply_filter": r.reply_filter, "skip_keywords": r.skip_keywords,
        "daily_cap": r.daily_cap, "min_gap_seconds": r.min_gap_seconds,
        "max_per_run": r.max_per_run, "interval_seconds": r.interval_seconds,
        "enabled": r.enabled, "last_error": r.last_error,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
    }


async def _resolve_rule_target(platform: str, mode: str, target_kind: str, target: str):
    """把 mode/target_kind/target 解析成 (kind, sec_uid, aweme_id, keyword, xsec_token)。
    解析失败抛 HTTPException。POST 与 PUT(改目标)共用。"""
    sec_uid = aweme_id = keyword = xsec_token = ""
    if mode == "auto_comment":
        kind = target_kind if target_kind in ("keyword", "creator") else "keyword"
        if kind == "keyword":
            keyword = (target or "").strip()
            if not keyword:
                raise HTTPException(400, "请填写要评论的搜索关键词")
            if platform in ("douyin", "kuaishou"):
                pn = "快手" if platform == "kuaishou" else "抖音"
                raise HTTPException(400, f"{pn}暂不支持关键词发现,请用「创作者」模式")
        else:
            if platform == "xhs":
                ref = await xhs_resolve_user(target, cfg.engine.user_agent)
                if not ref:
                    raise HTTPException(400, "无法解析小红书创作者(主页链接 / xhslink / user_id)")
                sec_uid, xsec_token = ref.user_id, ref.xsec_token
            elif platform == "kuaishou":
                sec_uid = await resolve_ks_user_id(target, cfg.engine.user_agent)
                if not sec_uid:
                    raise HTTPException(400, "无法解析快手创作者(主页链接 / 短链 / user_id)")
            else:
                sec_uid = await resolve_sec_uid(target, cfg.engine.user_agent)
                if not sec_uid:
                    raise HTTPException(400, "无法解析 sec_uid(主页链接 / 短链 / sec_uid)")
    else:
        kind = target_kind if target_kind in ("self", "work") else "self"
        if kind == "work":
            if platform == "xhs":
                ref = await xhs_resolve_note(target, cfg.engine.user_agent)
                if not ref:
                    raise HTTPException(400, "无法解析小红书笔记(explore 链接 / xhslink / note_id)")
                aweme_id, xsec_token = ref.note_id, ref.xsec_token
            elif platform == "kuaishou":
                aweme_id = await resolve_ks_photo_id(target, cfg.engine.user_agent)
                if not aweme_id:
                    raise HTTPException(400, "无法解析快手作品 id(作品链接 / 短链 / photo_id)")
            else:
                aweme_id = await resolve_aweme_id(target, cfg.engine.user_agent)
                if not aweme_id:
                    raise HTTPException(400, "无法解析作品 id(作品链接 / 短链 / 数字 id)")
    return kind, sec_uid, aweme_id, keyword, xsec_token


def _task_dict(t: CommentTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "rule_id": t.rule_id,
        "account_id": t.account_id, "aweme_id": t.aweme_id,
        "target_comment_id": t.target_comment_id, "target_nick": t.target_nick,
        "target_text": getattr(t, "target_text", ""),
        "content": t.content, "status": t.status, "result": t.result,
        "error": t.error, "method": t.method,
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "done_at": t.done_at.isoformat() if t.done_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@router.get("/api/comment-rules")
async def list_comment_rules(platform: str | None = None):
    with get_session() as s:
        q = select(CommentRule)
        if platform:
            q = q.where(CommentRule.platform == platform)
        return [_rule_dict(r) for r in s.exec(q.order_by(CommentRule.id.desc())).all()]


@router.post("/api/comment-rules")
async def add_comment_rule(body: CommentRuleIn):
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou") else "douyin"
    mode = body.mode if body.mode in ("auto_reply", "auto_comment") else "auto_reply"
    templates = [t.strip() for t in body.templates if t.strip()]
    if not templates:
        raise HTTPException(400, "请至少配置一条文案模板(AI 生成失败时回退用)")
    _pn = {"xhs": "小红书", "kuaishou": "快手"}.get(platform, "抖音")
    with get_session() as s:
        acc = s.get(DouyinAccount, body.account_id)
        if not acc or acc.platform != platform:
            raise HTTPException(400, f"请选择一个已登录的{_pn}账号")
        if not (acc.storage_state or acc.creator_storage_state):
            raise HTTPException(400, "该账号未登录,发评论需要登录态")

    kind, sec_uid, aweme_id, keyword, xsec_token = await _resolve_rule_target(
        platform, mode, body.target_kind, body.target)

    with get_session() as s:
        r = CommentRule(
            platform=platform, name=body.name or ("自动回复" if mode == "auto_reply" else "自动评论"),
            mode=mode, account_id=body.account_id, target_kind=kind,
            keyword=keyword, sec_uid=sec_uid, aweme_id=aweme_id, xsec_token=xsec_token,
            templates=json.dumps(templates, ensure_ascii=False), use_ai=body.use_ai,
            require_review=body.require_review,
            reply_filter=body.reply_filter.strip(), skip_keywords=body.skip_keywords.strip(),
            daily_cap=max(0, body.daily_cap), min_gap_seconds=max(1, body.min_gap_seconds),
            max_per_run=max(1, body.max_per_run),
            interval_seconds=max(60, body.interval_seconds), enabled=body.enabled)
        s.add(r); s.commit(); s.refresh(r)
        return _rule_dict(r)


@router.put("/api/comment-rules/{rid}")
async def update_comment_rule(rid: int, body: CommentRuleUpdate):
    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            raise HTTPException(404)
        platform = r.platform

    # 改账号:校验平台一致 + 已登录
    if body.account_id is not None:
        with get_session() as s:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != platform:
                raise HTTPException(400, "账号无效或与规则平台不一致")
            if not (acc.storage_state or acc.creator_storage_state):
                raise HTTPException(400, "该账号未登录,发评论需要登录态")

    # 改目标:mode/target_kind/target 任一传入则整体重解析
    new_target = None
    if body.mode is not None or body.target_kind is not None or body.target is not None:
        with get_session() as s:
            r = s.get(CommentRule, rid)
            mode = body.mode if body.mode in ("auto_reply", "auto_comment") else r.mode
            tk = body.target_kind if body.target_kind is not None else r.target_kind
            tgt = body.target if body.target is not None else ""
        new_target = (mode, *await _resolve_rule_target(platform, mode, tk, tgt))

    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            raise HTTPException(404)
        if body.account_id is not None:
            r.account_id = body.account_id
        if new_target is not None:
            r.mode, r.target_kind, r.sec_uid, r.aweme_id, r.keyword, r.xsec_token = new_target
        if body.name is not None:
            r.name = body.name
        if body.templates is not None:
            tps = [t.strip() for t in body.templates if t.strip()]
            if not tps:
                raise HTTPException(400, "文案模板不能为空")
            r.templates = json.dumps(tps, ensure_ascii=False)
        if body.use_ai is not None:
            r.use_ai = body.use_ai
        if body.require_review is not None:
            r.require_review = body.require_review
        if body.reply_filter is not None:
            r.reply_filter = body.reply_filter.strip()
        if body.skip_keywords is not None:
            r.skip_keywords = body.skip_keywords.strip()
        if body.daily_cap is not None:
            r.daily_cap = max(0, body.daily_cap)
        if body.min_gap_seconds is not None:
            r.min_gap_seconds = max(1, body.min_gap_seconds)
        if body.max_per_run is not None:
            r.max_per_run = max(1, body.max_per_run)
        if body.interval_seconds is not None:
            r.interval_seconds = max(60, body.interval_seconds)
        if body.enabled is not None:
            r.enabled = body.enabled
        s.add(r); s.commit(); s.refresh(r)
        return _rule_dict(r)


@router.delete("/api/comment-rules/{rid}")
async def del_comment_rule(rid: int, with_tasks: bool = True):
    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            return {"ok": True}
        if with_tasks:
            for t in s.exec(select(CommentTask).where(CommentTask.rule_id == rid)).all():
                s.delete(t)
        s.delete(r); s.commit()
    return {"ok": True}


@router.post("/api/comment-rules/{rid}/run-now")
async def run_comment_rule_now(rid: int):
    if not rt.engine:
        raise HTTPException(503, "引擎未就绪")
    return await rt.engine.run_comment_rule(rid)


@router.get("/api/comment-tasks")
async def list_comment_tasks(platform: str | None = None, rule_id: int | None = None,
                             status: str | None = None, limit: int = 200):
    with get_session() as s:
        q = select(CommentTask)
        if platform:
            q = q.where(CommentTask.platform == platform)
        if rule_id is not None:
            q = q.where(CommentTask.rule_id == rule_id)
        if status:
            q = q.where(CommentTask.status == status)
        rows = s.exec(q.order_by(CommentTask.id.desc()).limit(limit)).all()
        return [_task_dict(t) for t in rows]


@router.post("/api/comment-tasks/{tid}/run-now")
async def run_comment_task_now(tid: int):
    if not rt.engine:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status in ("done", "doing"):
            raise HTTPException(400, f"任务状态为 {t.status}")
        t.status = "pending"; t.scheduled_at = None; t.error = ""
        s.add(t); s.commit()
    return await rt.engine.execute_comment_task(tid)


@router.post("/api/comment-tasks/{tid}/cancel")
async def cancel_comment_task(tid: int):
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status in ("draft", "pending", "failed"):
            t.status = "canceled"
            s.add(t); s.commit()
    return {"ok": True}


class IdsIn2(BaseModel):
    ids: list[int] = []


class TaskContentIn(BaseModel):
    content: str


@router.put("/api/comment-tasks/{tid}")
async def edit_comment_task(tid: int, body: TaskContentIn):
    """编辑草稿/待发任务的文案(草稿审核时人工微调用)。"""
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "文案不能为空")
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status not in ("draft", "pending", "failed"):
            raise HTTPException(400, f"任务状态为 {t.status},不可编辑")
        t.content = content[:200]
        s.add(t); s.commit(); s.refresh(t)
        return _task_dict(t)


def _approve_one(s, t) -> bool:
    """把 draft 任务转为 pending(通过审核)。返回是否改动。"""
    if t and t.status == "draft":
        t.status = "pending"; t.error = ""; t.scheduled_at = None
        s.add(t)
        return True
    return False


@router.post("/api/comment-tasks/{tid}/approve")
async def approve_comment_task(tid: int):
    """通过单条草稿:draft -> pending,引擎随后按节流自动发出。"""
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if not _approve_one(s, t):
            raise HTTPException(400, f"任务状态为 {t.status},非草稿")
        s.commit()
    return {"ok": True}


@router.post("/api/comment-tasks/batch-approve")
async def batch_approve_comment_tasks(body: IdsIn2):
    """批量通过草稿。ids 为空时通过该平台所有草稿(由前端传 platform 过滤的 ids 更精确)。"""
    n = 0
    with get_session() as s:
        if body.ids:
            for tid in body.ids:
                if _approve_one(s, s.get(CommentTask, tid)):
                    n += 1
        else:
            for t in s.exec(select(CommentTask).where(CommentTask.status == "draft")).all():
                if _approve_one(s, t):
                    n += 1
        s.commit()
    return {"ok": True, "approved": n}


@router.delete("/api/comment-tasks/{tid}")
async def del_comment_task(tid: int):
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if t:
            s.delete(t); s.commit()
    return {"ok": True}


@router.post("/api/comment-tasks/batch-delete")
async def batch_del_comment_tasks(body: IdsIn2):
    n = 0
    with get_session() as s:
        for tid in body.ids:
            t = s.get(CommentTask, tid)
            if t:
                s.delete(t); n += 1
        s.commit()
    return {"ok": True, "deleted": n}

