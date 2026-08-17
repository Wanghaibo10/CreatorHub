"""本账号数据 API(作品/评论/弹幕/关注/私信/统计/写操作队列)。

从 main.py 抽出(2026-08-17 模块化)。抓取走 browser/ 的账号中心,
写操作执行在 engine/monitor.py,读操作统一过 services/account_ops.py 闸门。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import select

from application.browser import (fetch_account_works, fetch_dm_conversations, fetch_dm_history, fetch_follows)
from moss.common.db import get_session
from moss.common.logging_setup import get_logger
from moss.model import (AccountActionTask, AccountStatSnapshot, AccountWork, CommentRecord, DanmakuRecord, DmConversation, DmMessage, DouyinAccount, FollowEdge)
from moss.core.risk import OperationKind
from moss.core.runtime import rt
from app.service.account_ops import _run_account_read
from app.service.watch_data import _comment_dict, _danmaku_dict

router = APIRouter(tags=["account-data"])
log = get_logger("routers.account_data")


def _work_dict(w: AccountWork) -> dict:
    return {
        "id": w.id, "platform": w.platform, "account_id": w.account_id,
        "item_id": w.item_id, "desc": w.desc, "media_type": w.media_type,
        "cover_url": w.cover_url, "create_time": w.create_time,
        "like_count": w.like_count, "comment_count": w.comment_count,
        "collect_count": w.collect_count, "share_count": w.share_count,
        "play_count": w.play_count, "status": w.status,
        "fetched_at": w.fetched_at.isoformat() if w.fetched_at else None,
    }


@router.get("/api/account-works")
async def list_account_works(account_id: int, limit: int = 200):
    with get_session() as s:
        q = (select(AccountWork).where(AccountWork.account_id == account_id)
             .order_by(AccountWork.create_time.desc()).limit(limit))
        return [_work_dict(w) for w in s.exec(q).all()]


@router.post("/api/accounts/{account_id}/works/sync")
async def sync_account_works(account_id: int):
    """打开账号自己的主页,拦截抓取本账号已发布作品,落库(upsert)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    if rt.engine is None:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if acc.status == "invalid":
            raise HTTPException(400, "账号登录态已失效")
        if rt.engine._proxy_bad(acc):
            raise HTTPException(400, "账号代理不可用")
        platform = acc.platform
        uid = acc.sec_uid or ""
        identity = rt.browser.identity_for(acc)
    async def _fetch():
        return await fetch_account_works(rt.browser, identity, platform, uid)

    items, err = await rt.engine.guarded_read_pair(
        account_id, OperationKind.READ_LIGHT, f"account-works:{account_id}",
        _fetch, empty_result=[])
    if err.startswith("risk_deferred:"):
        return {"ok": True, "fetched": 0, "added": 0, "skipped": True,
                "reason": err.split(":", 1)[-1]}
    if not items:
        if err and err.startswith("missing_uid"):
            raise HTTPException(400, err.split(":", 1)[-1])
        raise HTTPException(400, f"未抓到作品:{err or '可能登录态失效/无公开作品'}"
                                 "(详情见服务端控制台日志)")
    now = datetime.utcnow()
    added = 0
    with get_session() as s:
        for w in items:
            existing = s.exec(select(AccountWork).where(
                AccountWork.account_id == account_id,
                AccountWork.item_id == w["item_id"])).first()
            if existing:
                for k, v in w.items():
                    setattr(existing, k, v)
                existing.fetched_at = now
                s.add(existing)
            else:
                s.add(AccountWork(platform=platform, account_id=account_id,
                                  fetched_at=now, **w))
                added += 1
        s.commit()
    return {"ok": True, "fetched": len(items), "added": added}


@router.get("/api/account-stats/{account_id}")
async def account_stats(account_id: int, days: int = 30):
    """返回该账号近 days 天的每日快照趋势 + 当前本账号作品的单篇互动明细。
    快照由引擎在账号体检/作品健康时写入(见 EngineConfig.work_health_*)。"""
    days = max(1, min(days, 180))
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        snaps = s.exec(select(AccountStatSnapshot)
                       .where(AccountStatSnapshot.account_id == account_id)
                       .order_by(AccountStatSnapshot.date.desc()).limit(days)).all()
        works = s.exec(select(AccountWork)
                       .where(AccountWork.account_id == account_id)
                       .order_by(AccountWork.create_time.desc()).limit(50)).all()
    trend = [{"date": x.date, "follower_count": x.follower_count,
              "aweme_count": x.aweme_count, "total_like": x.total_like,
              "total_comment": x.total_comment, "total_play": x.total_play}
             for x in reversed(snaps)]
    latest = trend[-1] if trend else {}
    prev = trend[-2] if len(trend) >= 2 else {}
    fans_delta = (latest.get("follower_count", 0) - prev.get("follower_count", 0)
                  if prev else 0)
    return {
        "account": {"id": acc.id, "platform": acc.platform, "nickname": acc.nickname,
                    "follower_count": acc.follower_count, "aweme_count": acc.aweme_count},
        "fans_delta": fans_delta,
        "trend": trend,
        "works": [_work_dict(w) for w in works],
    }


@router.get("/api/account-works/{work_id}/comments")
async def list_work_comments(work_id: int, limit: int = 300):
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        item_id = w.item_id
        rows = s.exec(select(CommentRecord).where(
            CommentRecord.watch_id == 0,
            CommentRecord.aweme_id == item_id)
            .order_by(CommentRecord.id.desc()).limit(limit)).all()
        return [_comment_dict(c) for c in rows]


@router.post("/api/account-works/{work_id}/comments/sync")
async def sync_work_comments(work_id: int):
    if rt.engine is None:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        platform, item_id = w.platform, w.item_id
        account_id, xsec_token = w.account_id, w.xsec_token
    res = await rt.engine.sync_work_comments(account_id, platform, item_id, xsec_token)
    if not res.get("ok") and not res.get("added"):
        raise HTTPException(400, f"抓评论失败:{res.get('error') or '未知'}"
                                 "(详情见服务端控制台日志)")
    return res


@router.get("/api/account-works/{work_id}/danmaku")
async def list_work_danmaku(work_id: int, limit: int = 300):
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        rows = s.exec(select(DanmakuRecord).where(
            DanmakuRecord.watch_id == 0,
            DanmakuRecord.aweme_id == w.item_id)
            .order_by(DanmakuRecord.id.desc()).limit(limit)).all()
        return [_danmaku_dict(row) for row in rows]


@router.post("/api/account-works/{work_id}/danmaku/sync")
async def sync_work_danmaku(work_id: int):
    if rt.engine is None:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        platform, item_id, account_id = w.platform, w.item_id, w.account_id
    res = await rt.engine.sync_work_danmaku(account_id, platform, item_id)
    if not res.get("ok") and not res.get("added"):
        raise HTTPException(400, f"抓弹幕失败:{res.get('error') or '未知'}"
                                 "(详情见服务端控制台日志)")
    return res


def _follow_dict(f: FollowEdge) -> dict:
    return {
        "id": f.id, "platform": f.platform, "account_id": f.account_id,
        "direction": f.direction, "uid": f.uid, "sec_uid": f.sec_uid,
        "nickname": f.nickname, "avatar": f.avatar, "signature": f.signature,
        "is_mutual": f.is_mutual, "is_following": f.is_following,
        "fetched_at": f.fetched_at.isoformat() if f.fetched_at else None,
    }


@router.get("/api/follows")
async def list_follows(account_id: int, direction: str = "following", limit: int = 500):
    with get_session() as s:
        q = (select(FollowEdge).where(FollowEdge.account_id == account_id,
                                      FollowEdge.direction == direction)
             .order_by(FollowEdge.id.desc()).limit(limit))
        return [_follow_dict(f) for f in s.exec(q).all()]


@router.post("/api/accounts/{account_id}/follows/sync")
async def sync_follows(account_id: int, direction: str = "following"):
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    if direction not in ("following", "fan"):
        raise HTTPException(400, "direction 仅支持 following | fan")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        uid = acc.sec_uid or ""
        identity = rt.browser.identity_for(acc)
        known = {f.uid for f in s.exec(select(FollowEdge).where(
            FollowEdge.account_id == account_id,
            FollowEdge.direction == direction)).all()}
    # 抖音优先直连(following/follower list 分页,比弹窗滚动抓得全);失败再回退浏览器拦截
    users, err = [], ""
    attempted_direct = platform == "douyin" and rt.engine is not None
    if attempted_direct:
        try:
            users, derr = await rt.engine.fetch_douyin_follows_direct(account_id, direction)
        except Exception as e:
            users, derr = [], repr(e)
        if derr.startswith("risk_deferred:"):
            return {"ok": True, "fetched": 0, "added": 0, "skipped": True,
                    "reason": derr.split(":", 1)[-1]}
        if not users and derr not in ("", "empty"):
            log.info(f"douyin direct 空({derr}),回退浏览器拦截")
    allow_browser_fallback = (not attempted_direct or derr == "no_cookie")
    if not users and allow_browser_fallback:
        if rt.engine is not None:
            async def _fetch_browser_follows():
                return await fetch_follows(
                    rt.browser, identity, platform, uid, direction, known)

            users, err = await rt.engine.guarded_read_pair(
                account_id, OperationKind.READ_HEAVY,
                f"follows-browser:{account_id}:{direction}",
                _fetch_browser_follows, empty_result=[])
            if err.startswith("risk_deferred:"):
                return {"ok": True, "fetched": 0, "added": 0,
                        "skipped": True, "reason": err.split(":", 1)[-1]}
        else:
            users, err = await fetch_follows(
                rt.browser, identity, platform, uid, direction, known)
    # 仅在登录态/缺 id 这类硬错误时报错;抓到 0 条不报错(可能确实没有,或接口待标定)
    if err and err.startswith("missing_uid"):
        raise HTTPException(400, err.split(":", 1)[-1])
    if err and err.startswith("logged_out"):
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    now = datetime.utcnow()
    with get_session() as s:
        # 快照式替换:先清掉该账号该方向旧数据(含历史误抓的 JS 模块垃圾),再写入本次精确快照
        for old in s.exec(select(FollowEdge).where(
                FollowEdge.account_id == account_id,
                FollowEdge.direction == direction)).all():
            s.delete(old)
        for u in users:
            s.add(FollowEdge(platform=platform, account_id=account_id,
                             direction=direction, fetched_at=now, **u))
        s.commit()
    return {"ok": True, "fetched": len(users), "added": len(users)}


def _conv_dict(c: DmConversation) -> dict:
    return {
        "id": c.id, "platform": c.platform, "account_id": c.account_id,
        "conv_id": c.conv_id, "peer_uid": c.peer_uid, "peer_sec_uid": c.peer_sec_uid,
        "peer_nickname": c.peer_nickname, "peer_avatar": c.peer_avatar,
        "last_text": c.last_text, "last_time": c.last_time,
        "unread_count": c.unread_count,
        "fetched_at": c.fetched_at.isoformat() if c.fetched_at else None,
    }


@router.get("/api/dm/conversations")
async def list_dm_conversations(account_id: int, limit: int = 200):
    with get_session() as s:
        q = (select(DmConversation).where(DmConversation.account_id == account_id)
             .order_by(DmConversation.last_time.desc()).limit(limit))
        return [_conv_dict(c) for c in s.exec(q).all()]


@router.get("/api/dm/messages")
async def list_dm_messages(account_id: int, conv_id: str, limit: int = 200):
    with get_session() as s:
        q = (select(DmMessage).where(DmMessage.account_id == account_id,
                                     DmMessage.conv_id == conv_id)
             .order_by(DmMessage.create_time.asc()).limit(limit))

        def _card(m):
            if not m.raw_json:
                return None
            try:
                return json.loads(m.raw_json)
            except Exception:
                return None
        return [{"id": m.id, "direction": m.direction, "text": m.text,
                 "msg_type": m.msg_type, "create_time": m.create_time,
                 "card": _card(m)}
                for m in s.exec(q).all()]


@router.post("/api/accounts/{account_id}/dm/sync")
async def sync_dm(account_id: int):
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        identity = rt.browser.identity_for(acc)
    if rt.engine is None:
        raise HTTPException(503, "引擎未就绪")
    async def _fetch_conversations():
        return await fetch_dm_conversations(rt.browser, identity, platform)

    convs, err = await rt.engine.guarded_read_pair(
        account_id, OperationKind.READ_HEAVY, f"dm:{account_id}",
        _fetch_conversations, empty_result=[])
    if err.startswith("risk_deferred:"):
        return {"ok": True, "fetched": 0, "added": 0, "skipped": True,
                "reason": err.split(":", 1)[-1]}
    if err and err.startswith("logged_out"):
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    # 小红书网页端私信未开放(entry visible=false)等硬限制:直接把原因回给前端
    if not convs and err:
        raise HTTPException(400, err)
    now = datetime.utcnow()
    with get_session() as s:
        # 快照式替换:清掉旧会话(含历史误抓的 JS 模块垃圾),写入本次抓到的
        for old in s.exec(select(DmConversation).where(
                DmConversation.account_id == account_id)).all():
            s.delete(old)
        # 会话最后一条消息也快照式重写(仅 last:<conv> 这条,历史记录由按需抓取补)
        for old in s.exec(select(DmMessage).where(
                DmMessage.account_id == account_id,
                DmMessage.msg_id.like("last:%"))).all():
            s.delete(old)
        msgs = 0
        for c in convs:
            s.add(DmConversation(platform=platform, account_id=account_id,
                                 fetched_at=now, **c))
            # get_message_by_init 已带每会话最后一条消息:落成 thread 里的一条,
            # 让「点开会话」不再空。方向由 last_sender_uid==self_uid 判定。
            meta = {}
            try:
                meta = json.loads(c.get("raw_json") or "{}")
            except Exception:
                meta = {}
            if c.get("last_text"):
                direction = ("out" if meta.get("last_sender_uid")
                             and meta.get("last_sender_uid") == meta.get("self_uid")
                             else "in")
                s.add(DmMessage(
                    platform=platform, account_id=account_id, conv_id=c["conv_id"],
                    msg_id="last:" + c["conv_id"], direction=direction,
                    msg_type="text", text=c["last_text"],
                    create_time=c.get("last_time") or 0))
                msgs += 1
        # 顺带存账号自身 uid(= IM device_id,实时接收 WS 要用);从任一会话的 self_uid 取
        self_uid = ""
        for c in convs:
            try:
                self_uid = (json.loads(c.get("raw_json") or "{}")).get("self_uid", "")
            except Exception:
                self_uid = ""
            if self_uid:
                break
        if self_uid:
            acc2 = s.get(DouyinAccount, account_id)
            if acc2 and acc2.uid != self_uid:
                acc2.uid = self_uid
                s.add(acc2)
        s.commit()
    return {"ok": True, "fetched": len(convs), "added": len(convs), "messages": msgs}


@router.get("/api/dm/stream")
async def dm_stream(account_id: int):
    """私信实时事件流(SSE)。前端打开 DM 面板时订阅;订阅即为该账号拉起 frontier-im
    WS 长连接,新消息实时推来(也已入库)。最后一个订阅断开时自动停连。"""
    if rt.im_receiver is None:
        raise HTTPException(503, "实时接收未就绪")
    q = await rt.im_receiver.subscribe(account_id)

    async def gen():
        try:
            yield "retry: 3000\nevent: ready\ndata: {}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"     # 心跳,防代理断流
        except asyncio.CancelledError:
            pass
        finally:
            rt.im_receiver.unsubscribe(account_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/accounts/{account_id}/dm/conversations/{conv_id:path}/mark-read")
async def mark_dm_read(account_id: int, conv_id: str):
    """标记会话已读:清本地未读计数(红点)。"""
    with get_session() as s:
        conv = s.exec(select(DmConversation).where(
            DmConversation.account_id == account_id,
            DmConversation.conv_id == conv_id)).first()
        if conv and conv.unread_count:
            conv.unread_count = 0
            s.add(conv); s.commit()
    return {"ok": True}


@router.post("/api/accounts/{account_id}/dm/conversations/{conv_id:path}/fetch-history")
async def fetch_dm_conversation_history(account_id: int, conv_id: str,
                                        cursor: int = 0, debug: bool = False):
    """无头抓单个会话历史消息(imapi get_by_conversation,纯 cookie),落库 DmMessage。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        identity = rt.browser.identity_for(acc)
        conv = s.exec(select(DmConversation).where(
            DmConversation.account_id == account_id,
            DmConversation.conv_id == conv_id)).first()
        if not conv:
            raise HTTPException(404, "会话不存在(先同步会话列表)")
        short_id, self_uid = conv.conv_short_id, ""
        try:
            self_uid = (json.loads(conv.raw_json or "{}")).get("self_uid", "")
        except Exception:
            pass
    if not short_id:
        raise HTTPException(400, "该会话缺 conversation_short_id,请重新同步会话列表")
    if rt.engine is None:
        raise HTTPException(503, "引擎未就绪")
    async def _fetch_history():
        return await fetch_dm_history(
            rt.browser, identity, platform, conv_id, short_id,
            conv_type=1, cursor=cursor, debug=debug)
    parsed, err = await rt.engine.guarded_read_pair(
        account_id, OperationKind.READ_HEAVY,
        f"dm-history:{account_id}:{conv_id}", _fetch_history,
        empty_result={})
    if err.startswith("risk_deferred:"):
        return {"ok": True, "messages": 0, "added": 0, "skipped": True,
                "reason": err.split(":", 1)[-1]}
    if err:
        raise HTTPException(400, err)
    msgs = parsed.get("messages", [])
    added = 0
    with get_session() as s:
        # 按会话快照重写:拉到消息就清掉该会话旧消息(含 last:<conv> 占位、旧错时间戳),
        # 再插本次窗口。get_by_conversation 每次返回最近一窗,快照式最简单且能纠正旧数据。
        if msgs:
            for old in s.exec(select(DmMessage).where(
                    DmMessage.account_id == account_id,
                    DmMessage.conv_id == conv_id)).all():
                s.delete(old)
        seen = set()
        for m in msgs:
            mid = m.get("server_msg_id") or ""
            if not mid or mid in seen:
                continue
            seen.add(mid)
            direction = ("out" if self_uid and m.get("sender_uid") == self_uid
                         else "in")
            card = m.get("card")
            s.add(DmMessage(
                platform=platform, account_id=account_id, conv_id=conv_id,
                msg_id=mid, direction=direction,
                msg_type=("video" if card else
                          "text" if m.get("text") else str(m.get("msg_type") or "")),
                text=m.get("text") or "", create_time=int(m.get("create_time") or 0),
                raw_json=json.dumps(card, ensure_ascii=False) if card else ""))
            added += 1
        s.commit()
    out = {"ok": True, "fetched": len(msgs), "added": added,
           "next_cursor": parsed.get("next_cursor"), "has_more": parsed.get("has_more")}
    if debug:   # 非文本消息(分享视频=8/图片=27/语音=17...)回原始 content,标定字段用
        out["media_samples"] = [
            {"msg_type": m.get("msg_type"), "text": m.get("text"),
             "content": m.get("content")}
            for m in msgs if m.get("msg_type") not in (7, 0)]
    return out


class ActionIn(BaseModel):
    account_id: int
    action: str                 # follow | unfollow | send_dm
    target_uid: str = ""
    target_sec_uid: str = ""
    target_nick: str = ""
    conv_id: str = ""
    content: str = ""
    run_now: bool = False        # True=立即执行;False=入队(引擎节流后执行)


def _action_dict(t: AccountActionTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "account_id": t.account_id,
        "action": t.action, "target_uid": t.target_uid, "target_nick": t.target_nick,
        "content": t.content, "status": t.status, "result": t.result,
        "error": t.error, "created_at": t.created_at.isoformat() if t.created_at else None,
        "done_at": t.done_at.isoformat() if t.done_at else None,
    }


async def _exec_action(task_id: int) -> tuple[bool, str]:
    """立即执行一条写操作:委托引擎(带每账号串行锁,避免同号并发开窗)。"""
    if rt.engine is None:
        raise HTTPException(503, "引擎未就绪")
    res = await rt.engine.execute_action_task(task_id)
    return bool(res.get("ok")), (res.get("error") or "")


@router.get("/api/account-actions")
async def list_account_actions(account_id: int | None = None, limit: int = 100):
    with get_session() as s:
        q = select(AccountActionTask)
        if account_id:
            q = q.where(AccountActionTask.account_id == account_id)
        q = q.order_by(AccountActionTask.id.desc()).limit(limit)
        return [_action_dict(t) for t in s.exec(q).all()]


@router.post("/api/account-actions")
async def create_account_action(body: ActionIn):
    if body.action not in ("follow", "unfollow", "send_dm"):
        raise HTTPException(400, "action 仅支持 follow | unfollow | send_dm")
    if body.action == "send_dm" and not body.content.strip():
        raise HTTPException(400, "发私信需填写内容")
    if not (body.target_uid or body.target_sec_uid):
        raise HTTPException(400, "缺目标用户")
    with get_session() as s:
        acc = s.get(DouyinAccount, body.account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        t = AccountActionTask(
            platform=acc.platform, account_id=body.account_id, action=body.action,
            target_uid=body.target_uid, target_sec_uid=body.target_sec_uid,
            target_nick=body.target_nick, conv_id=body.conv_id,
            content=body.content.strip(), status="pending")
        s.add(t); s.commit(); s.refresh(t)
        task_id = t.id
    if body.run_now:
        ok, detail = await _exec_action(task_id)
        if not ok:
            raise HTTPException(400, f"执行失败:{detail}")
        return {"ok": True, "id": task_id, "ran": True}
    return {"ok": True, "id": task_id, "ran": False}


@router.post("/api/account-actions/{task_id}/run-now")
async def run_account_action(task_id: int):
    ok, detail = await _exec_action(task_id)
    if not ok:
        raise HTTPException(400, f"执行失败:{detail}")
    return {"ok": True}


@router.post("/api/account-actions/{task_id}/cancel")
async def cancel_account_action(task_id: int):
    with get_session() as s:
        t = s.get(AccountActionTask, task_id)
        if not t:
            raise HTTPException(404, "任务不存在")
        if t.status in ("done", "doing"):
            raise HTTPException(400, "该任务已执行,无法取消")
        t.status = "canceled"; s.add(t); s.commit()
    return {"ok": True}
