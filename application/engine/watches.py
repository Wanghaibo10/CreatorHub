"""评论/弹幕监控:独立 watch 实体的扫描、入库与通知。

MonitorEngine 的 Mixin(2026-08-17 从 monitor.py 拆出):方法仍属同一个引擎
实例,self 状态共享不变;拆分只为按职责分文件。
"""
from __future__ import annotations

from __future__ import annotations
import time
from datetime import datetime
from sqlmodel import select
from application.browser import (fetch_videos, fetch_comments, fetch_creator_comments, fetch_danmaku, fetch_creator_danmaku, fetch_ks_videos, fetch_ks_comments, fetch_channels_comments)
from moss.common.db import get_session
from application.douyin import (parse_comment, parse_creator_comment, parse_danmaku, DouyinClient, cookie_from_state as dy_cookie_from_state)
from application.xhs import (parse_note_brief, parse_comment as parse_xhs_comment, flatten_comments as flatten_xhs_comments, parse_self_user as parse_xhs_self_user, XhsApiClient, XhsApiError, cookie_str_from_state, has_a1)
from application.kuaishou import (parse_ks_feed, parse_ks_comment, flatten_ks_comments, parse_self_user as parse_ks_self_user)
from application.channels import parse_channels_comment, flatten_channels_comments
from moss.model import (CommentRecord, CommentWatch, DanmakuWatch, DanmakuRecord, DouyinAccount, NotificationChannel)
from moss.common.notifier import notify_all
from moss.core.risk import classify_platform_error, OperationKind, RiskCategory

from application.engine._helpers import _danmaku_matches, _loads, _loads_list, log


class WatchOps:
    async def _scan_danmaku_watches(self):
        due = []
        with get_session() as s:
            watches = s.exec(select(DanmakuWatch).where(
                DanmakuWatch.enabled == True)).all()  # noqa: E712
            for watch in watches:
                interval = watch.interval_seconds or self.cfg.engine.scan_interval_seconds
                if self._due(watch.last_scan_at, interval):
                    due.append(watch.id)
        for watch_id in due:
            await self.scan_danmaku_watch(watch_id)

    async def sync_work_danmaku(self, account_id: int, platform: str,
                                item_id: str) -> dict:
        """抓取本账号某条作品的弹幕，watch_id=0 表示账号管理入口。"""
        key = f"wd:{account_id}:{item_id}"
        if key in self._inflight:
            return {"ok": True, "fetched": 0, "added": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
            if not decision.allowed:
                return {"ok": True, "fetched": 0, "added": 0,
                        "skipped": True, "reason": decision.reason,
                        "next_allowed_at": (decision.next_allowed_at.isoformat()
                                            if decision.next_allowed_at else None)}
            async with self._operation_guard(
                    account_id, OperationKind.READ_HEAVY, fallback_key=key):
                decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
                if not decision.allowed:
                    return {"ok": True, "fetched": 0, "added": 0,
                            "skipped": True, "reason": decision.reason,
                            "next_allowed_at": (decision.next_allowed_at.isoformat()
                                                if decision.next_allowed_at else None)}
                with get_session() as s:
                    acc = s.get(DouyinAccount, account_id)
                    if not acc:
                        return {"ok": False, "error": "账号不存在"}
                    if platform != "douyin":
                        return {"ok": False, "error": "当前仅支持抖音短视频弹幕"}
                    if not acc.creator_storage_state:
                        return {"ok": False, "error": "需要先完成抖音创作者登录"}
                    identity = self.browser.identity_for(acc)
                    known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                        DanmakuRecord.watch_id == 0,
                        DanmakuRecord.aweme_id == item_id)).all())
                raw, err = await fetch_creator_danmaku(
                    self.browser, identity, known,
                    page_url=self.cfg.engine.creator_danmaku_url,
                    aweme_id=item_id,
                    max_scrolls=self.cfg.engine.danmaku_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources,
                )
                fresh = [p for p in (parse_danmaku(row, item_id) for row in raw) if p]
                added = 0
                with get_session() as s:
                    for item in fresh:
                        did = item.get("danmaku_id") or ""
                        if not did:
                            continue
                        exists = s.exec(select(DanmakuRecord).where(
                            DanmakuRecord.watch_id == 0,
                            DanmakuRecord.aweme_id == item_id,
                            DanmakuRecord.danmaku_id == did)).first()
                        if exists:
                            continue
                        s.add(DanmakuRecord(platform=platform, watch_id=0,
                                            aweme_id=item_id, source="creator",
                                            **{k: v for k, v in item.items()
                                               if k != "aweme_id"}))
                        added += 1
                    s.commit()
                result = {"ok": bool(added or not err), "fetched": len(fresh),
                          "added": added, "error": err}
                if result["ok"]:
                    self.risk.record_success(account_id, OperationKind.READ_HEAVY)
                elif err:
                    self.risk.record_failure(
                        account_id, OperationKind.READ_HEAVY, err)
                return result
        except Exception as e:
            log.warning("本账号作品弹幕抓取失败 %s/%s: %s", platform, item_id, e)
            self.risk.record_failure(account_id, OperationKind.READ_HEAVY, e)
            return {"ok": False, "fetched": 0, "added": 0, "error": repr(e)}
        finally:
            self._inflight.discard(key)

    async def scan_danmaku_watch(self, watch_id: int) -> dict:
        key = f"dw:{watch_id}"
        if key in self._inflight:
            return {"ok": True, "new_danmaku": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            with get_session() as s:
                watch = s.get(DanmakuWatch, watch_id)
                account_id = watch.account_id if watch else None
            return await self._guarded_read_dict(
                account_id, OperationKind.READ_HEAVY, key,
                lambda: self._scan_danmaku_watch_locked(watch_id))
        finally:
            self._inflight.discard(key)

    async def _scan_danmaku_watch_locked(self, watch_id: int) -> dict:
        with get_session() as s:
            watch = s.get(DanmakuWatch, watch_id)
            if not watch:
                return {"ok": False, "error": "watch not found"}
            first_scan = watch.last_scan_at is None
            kind, mode = watch.kind, watch.mode
            aweme_id, sec_uid = watch.aweme_id, watch.sec_uid
            name = watch.title or aweme_id or (sec_uid[:12] if sec_uid else "watch")
            identity = self.browser.anon_identity()
            has_creator = False
            if watch.account_id:
                acc = s.get(DouyinAccount, watch.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        msg = "账号代理标记为不可用(proxy bad),已跳过"
                        watch.last_scan_at = datetime.utcnow()
                        watch.last_error = msg
                        s.add(watch)
                        s.commit()
                        return {"ok": False, "new_danmaku": 0, "error": msg, "skipped": True}
                    has_creator = bool(acc.creator_storage_state)
                    identity = self.browser.identity_for(acc)

        if mode == "creator" and not has_creator:
            msg = "创作中心弹幕监控需要绑定已完成创作者登录的抖音账号"
            with get_session() as s:
                watch = s.get(DanmakuWatch, watch_id)
                if watch:
                    watch.last_scan_at = datetime.utcnow()
                    watch.last_error = msg
                    s.add(watch)
                    s.commit()
            return {"ok": False, "new_danmaku": 0, "error": msg}

        error = ""
        total_new = 0
        try:
            settings = {
                "recent_works": watch.recent_works or self.cfg.engine.danmaku_recent_works,
                "recent_days": watch.recent_days or self.cfg.engine.danmaku_recent_days,
                "max_scrolls": watch.max_scrolls or self.cfg.engine.danmaku_max_scrolls,
                "time_start_ms": max(0, watch.time_start_ms or 0),
                "time_end_ms": max(0, watch.time_end_ms or 0),
                "probe_step_seconds": watch.probe_step_seconds or self.cfg.engine.danmaku_probe_step_seconds,
                "max_probe_points": max(1, self.cfg.engine.danmaku_max_probe_points),
                "include_keywords": [str(x).strip() for x in _loads_list(watch.include_keywords) if str(x).strip()],
                "exclude_keywords": [str(x).strip() for x in _loads_list(watch.exclude_keywords) if str(x).strip()],
                "min_text_length": max(0, watch.min_text_length or 0),
                "max_text_length": max(0, watch.max_text_length or 0),
                "min_like_count": max(0, watch.min_like_count or 0),
                "max_records_per_scan": watch.max_records_per_scan or self.cfg.engine.danmaku_max_records_per_scan,
                "max_records_total": watch.max_records_total or self.cfg.engine.danmaku_max_records_total,
            }
            remaining = [settings["max_records_per_scan"] if settings["max_records_per_scan"] > 0 else None]

            def normalize_rows(raw_rows: list, default_id: str = "") -> list:
                parsed = [parse_danmaku(row, default_id)
                          for row in raw_rows if isinstance(row, dict)]
                parsed = [row for row in parsed if row and _danmaku_matches(row, settings)]
                parsed.sort(key=lambda row: (int(row.get("video_time_ms") or 0),
                                             str(row.get("danmaku_id") or "")))
                if remaining[0] is not None:
                    parsed = parsed[:remaining[0]]
                    remaining[0] -= len(parsed)
                return parsed

            raw_cap = (settings["max_records_per_scan"] * 5
                       if settings["max_records_per_scan"] else 0)
            source = "creator" if mode == "creator" else "public"
            if kind == "video":
                with get_session() as s:
                    known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                        DanmakuRecord.watch_id == watch_id,
                        DanmakuRecord.aweme_id == aweme_id)).all())
                if mode == "creator":
                    raw, error = await fetch_creator_danmaku(
                        self.browser, identity, known,
                        page_url=self.cfg.engine.creator_danmaku_url,
                        aweme_id=aweme_id,
                        max_scrolls=settings["max_scrolls"],
                        max_items=raw_cap,
                        block_media=self.cfg.engine.block_media_resources,
                    )
                else:
                    raw, error = await fetch_danmaku(
                        self.browser, identity, aweme_id, known,
                        max_rounds=max(1, min(settings["max_scrolls"], 2)),
                        start_ms=settings["time_start_ms"],
                        end_ms=settings["time_end_ms"],
                        step_seconds=settings["probe_step_seconds"],
                        max_points=settings["max_probe_points"],
                        max_items=raw_cap,
                        block_media=False,
                    )
                fresh = normalize_rows(raw, aweme_id)
                total_new = await self._ingest_danmaku(
                    watch_id, aweme_id, fresh, name, name, first_scan, source,
                    max_records_total=settings["max_records_total"])
            elif mode == "creator":
                with get_session() as s:
                    known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                        DanmakuRecord.watch_id == watch_id)).all())
                raw, error = await fetch_creator_danmaku(
                    self.browser, identity, known,
                    page_url=self.cfg.engine.creator_danmaku_url,
                    max_scrolls=settings["max_scrolls"],
                    max_items=raw_cap,
                    block_media=self.cfg.engine.block_media_resources,
                )
                grouped = {}
                for parsed in normalize_rows(raw):
                    if parsed and parsed.get("aweme_id"):
                        grouped.setdefault(parsed["aweme_id"], []).append(parsed)
                for aid, fresh in grouped.items():
                    total_new += await self._ingest_danmaku(
                        watch_id, aid, fresh, name, aid, first_scan, source,
                        max_records_total=settings["max_records_total"])
            else:
                items, _author, error = await fetch_videos(
                    self.browser, identity, sec_uid, set(),
                    max_scrolls=4, block_media=True)
                cutoff = int(time.time()) - settings["recent_days"] * 86400
                works = []
                for item in items:
                    aid = str(item.get("aweme_id") or "")
                    create_time = int(item.get("create_time") or 0)
                    if aid and (not cutoff or not create_time or create_time >= cutoff):
                        works.append((aid, item.get("desc") or ""))
                for aid, desc in works[:settings["recent_works"]]:
                    if remaining[0] is not None and remaining[0] <= 0:
                        break
                    with get_session() as s:
                        known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                            DanmakuRecord.watch_id == watch_id,
                            DanmakuRecord.aweme_id == aid)).all())
                    raw, item_error = await fetch_danmaku(
                        self.browser, identity, aid, known,
                        max_rounds=max(1, min(settings["max_scrolls"], 2)),
                        start_ms=settings["time_start_ms"],
                        end_ms=settings["time_end_ms"],
                        step_seconds=settings["probe_step_seconds"],
                        max_points=settings["max_probe_points"],
                        max_items=raw_cap,
                        block_media=False)
                    if item_error and not error:
                        error = item_error
                    fresh = normalize_rows(raw, aid)
                    total_new += await self._ingest_danmaku(
                        watch_id, aid, fresh, name, desc, first_scan, source,
                        max_records_total=settings["max_records_total"])
        except Exception as e:
            error = repr(e)
            log.warning("弹幕监控 %s 失败: %s", watch_id, e)

        with get_session() as s:
            watch = s.get(DanmakuWatch, watch_id)
            if watch:
                watch.last_scan_at = datetime.utcnow()
                watch.last_error = error
                watch.danmaku_count = len(s.exec(select(DanmakuRecord.id).where(
                    DanmakuRecord.watch_id == watch_id)).all())
                s.add(watch)
                s.commit()
        return {"ok": not error or total_new > 0,
                "new_danmaku": total_new, "error": error}

    async def _ingest_danmaku(self, watch_id: int, aweme_id: str, fresh: list,
                              name: str, work_desc: str, first_scan: bool,
                              source: str = "public",
                              max_records_total: int = 0) -> int:
        if not fresh and max_records_total <= 0:
            return 0
        added = []
        with get_session() as s:
            for item in fresh:
                aid = item.get("aweme_id") or aweme_id
                did = item.get("danmaku_id") or ""
                if not aid or not did:
                    continue
                exists = s.exec(select(DanmakuRecord).where(
                    DanmakuRecord.watch_id == watch_id,
                    DanmakuRecord.aweme_id == aid,
                    DanmakuRecord.danmaku_id == did)).first()
                if exists:
                    continue
                row = dict(item)
                row.pop("aweme_id", None)
                s.add(DanmakuRecord(platform="douyin", watch_id=watch_id,
                                    aweme_id=aid, source=source, **row))
                added.append(dict(item, aweme_id=aid))
            if max_records_total > 0:
                old_ids = s.exec(select(DanmakuRecord.id).where(
                    DanmakuRecord.watch_id == watch_id).order_by(
                        DanmakuRecord.created_at.desc(),
                        DanmakuRecord.id.desc()).offset(max_records_total)).all()
                for old_id in old_ids:
                    old = s.get(DanmakuRecord, old_id)
                    if old:
                        s.delete(old)
            s.commit()
        if not first_scan and added:
            await self._notify_danmaku(name, work_desc, added)
        return len(added)

    async def _scan_comment_watches(self):
        due = []
        with get_session() as s:
            ws = s.exec(select(CommentWatch).where(CommentWatch.enabled == True)).all()  # noqa: E712
            for w in ws:
                if self._due(w.last_scan_at, w.interval_seconds):
                    due.append(w.id)
        for wid in due:
            await self.scan_comment_watch(wid)

    async def sync_work_comments(self, account_id: int, platform: str, item_id: str,
                                 xsec_token: str = "") -> dict:
        """抓「本账号某作品」的评论并落库(watch_id=0 标记本账号来源)。
        抖音直连(comment/list 分页 + 回复,参考 CommentAll),小红书走签名直连客户端,
        快手走浏览器拦截。返回 {ok, fetched, added, error}。"""
        key = f"wc:{account_id}:{item_id}"
        if key in self._inflight:
            return {"ok": True, "fetched": 0, "added": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            return await self._guarded_read_dict(
                account_id, OperationKind.READ_HEAVY, key,
                lambda: self._sync_work_comments_locked(
                    account_id, platform, item_id, xsec_token))
        finally:
            self._inflight.discard(key)

    async def _sync_work_comments_locked(self, account_id, platform, item_id,
                                         xsec_token) -> dict:
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if not acc:
                return {"ok": False, "error": "账号不存在"}
            if acc.status == "invalid":
                return {"ok": False, "error": "账号登录态已失效"}
            if self._proxy_bad(acc):
                return {"ok": False, "error": "账号代理不可用"}
            state = acc.storage_state or acc.creator_storage_state or ""
            ua = acc.ua or self.cfg.engine.user_agent
            proxy = acc.proxy or ""
            identity = self.browser.identity_for(acc)
            known = set(s.exec(select(CommentRecord.comment_id).where(
                CommentRecord.watch_id == 0,
                CommentRecord.aweme_id == item_id)).all())
        fresh: list = []
        error = ""
        try:
            if platform == "douyin":
                cookie = dy_cookie_from_state(state)
                if not cookie:
                    return {"ok": False, "error": "账号无抖音登录态 Cookie,无法直连抓评论"}
                client = DouyinClient(cookie, ua,
                                      timeout=self.cfg.engine.request_timeout_seconds,
                                      proxy=proxy)
                raw = await client.fetch_all_comments(item_id)
                fresh = [c for c in (parse_comment(rc) for rc in raw)
                         if c and c["comment_id"] not in known]
            elif platform == "xhs":
                client = self._xhs_client(state, proxy)
                if client is None:
                    return {"ok": False, "error": "小红书账号缺 a1 Cookie,无法抓评论"}
                fresh = await self._xhs_fetch_comments(client, item_id, xsec_token, known)
            elif platform == "kuaishou":
                raw, err = await fetch_ks_comments(
                    self.browser, identity, item_id, known,
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources)
                error = err or ""
                fresh = [c for c in (parse_ks_comment(rc) for rc in flatten_ks_comments(raw))
                         if c and c["comment_id"] not in known]
            elif platform == "shipinhao":
                raw, err = await fetch_channels_comments(
                    self.browser, identity, item_id, known,
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources)
                error = err or ""
                fresh = [c for c in (parse_channels_comment(rc)
                                     for rc in flatten_channels_comments(raw))
                         if c and c["comment_id"] not in known]
            else:
                return {"ok": False, "error": f"不支持的平台:{platform}"}
        except XhsApiError as e:
            error = e
        except Exception as e:
            log.warning("本账号作品评论抓取失败 %s/%s: %s", platform, item_id, e)
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else repr(e))
            return {"ok": False, "error": error}
        # 去重落库(watch_id=0 = 本账号作品来源)
        added = 0
        with get_session() as s:
            for c in fresh:
                cid = c.get("comment_id")
                if not cid:
                    continue
                exists = s.exec(select(CommentRecord).where(
                    CommentRecord.watch_id == 0,
                    CommentRecord.aweme_id == item_id,
                    CommentRecord.comment_id == cid)).first()
                if exists:
                    continue
                s.add(CommentRecord(platform=platform, watch_id=0, aweme_id=item_id, **c))
                added += 1
            s.commit()
        return {"ok": not error or added > 0, "fetched": len(fresh),
                "added": added, "error": error}

    async def fetch_douyin_follows_direct(self, account_id: int, direction: str):
        return await self.guarded_read_pair(
            account_id, OperationKind.READ_HEAVY,
            f"follows:{account_id}:{direction}",
            lambda: self._fetch_douyin_follows_direct_locked(
                account_id, direction),
            empty_result=[])

    async def _fetch_douyin_follows_direct_locked(self, account_id: int, direction: str):
        """抖音关注/粉丝直连(following/follower list 分页,比弹窗滚动抓得全)。
        返回 (归一用户列表, error);拿不到时上层回退浏览器拦截,故失败无副作用。"""
        from application.browser.account_hub import _norm_follow_user
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if not acc:
                return [], "账号不存在"
            if acc.status == "invalid":
                return [], "账号登录态已失效"
            if self._proxy_bad(acc):
                return [], "账号代理不可用"
            state = acc.storage_state or acc.creator_storage_state or ""
            ua = acc.ua or self.cfg.engine.user_agent
            proxy = acc.proxy or ""
            sec_uid = acc.sec_uid or ""
        cookie = dy_cookie_from_state(state)
        if not cookie:
            return [], "no_cookie"
        client = DouyinClient(cookie, ua,
                              timeout=self.cfg.engine.request_timeout_seconds,
                              proxy=proxy)
        try:
            raw = await client.fetch_all_follows("", sec_uid, direction)
        except Exception as e:
            return [], repr(e)
        out = []
        for u in raw:
            n = _norm_follow_user(u, direction)
            if n:
                out.append(n)
        log.debug(f"[follow-direct] dir={direction} sec_uid={sec_uid} raw={len(raw)} "
              f"norm={len(out)}")
        return out, ("" if out else "empty")

    async def scan_comment_watch(self, watch_id: int) -> dict:
        key = f"cw:{watch_id}"
        if key in self._inflight:
            return {"ok": True, "new_comments": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            with get_session() as s:
                w = s.get(CommentWatch, watch_id)
                account_id = w.account_id if w else None
            return await self._guarded_read_dict(
                account_id, OperationKind.READ_HEAVY, key,
                lambda: self._scan_comment_watch_locked(watch_id))
        finally:
            self._inflight.discard(key)

    async def _scan_comment_watch_locked(self, watch_id: int) -> dict:
        with get_session() as s:
            w = s.get(CommentWatch, watch_id)
            if not w:
                return {"ok": False, "error": "watch not found"}
            first_scan = w.last_scan_at is None
            platform = w.platform
            kind, mode = w.kind, w.mode
            aweme_id, sec_uid = w.aweme_id, w.sec_uid
            xsec_token = w.xsec_token or ""
            name = w.title or aweme_id or (sec_uid[:12] if sec_uid else "watch")
            state = creator_state = proxy = ""
            identity = self.browser.anon_identity()
            has_creator = False
            if w.account_id:
                acc = s.get(DouyinAccount, w.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        msg = "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP"
                        w2 = s.get(CommentWatch, watch_id)
                        if w2:
                            w2.last_scan_at = datetime.utcnow()
                            w2.last_error = msg
                            s.add(w2); s.commit()
                        return {"ok": False, "new_comments": 0, "error": msg, "skipped": True}
                    state = acc.storage_state or ""
                    creator_state = acc.creator_storage_state or ""
                    proxy = acc.proxy or ""
                    has_creator = bool(creator_state)
                    identity = self.browser.identity_for(acc)

        error = ""
        total_new = 0
        author = None
        if platform == "xhs" and not state:
            msg = "小红书评论监控需要绑定一个已登录的小红书账号(笔记页需登录)"
            with get_session() as s:
                w = s.get(CommentWatch, watch_id)
                if w:
                    w.last_scan_at = datetime.utcnow()
                    w.last_error = msg
                    s.add(w); s.commit()
            return {"ok": False, "new_comments": 0, "error": msg}
        try:
            if platform == "xhs" and kind == "user":
                total_new, author = await self._cw_xhs_creator(watch_id, state, sec_uid,
                                                               xsec_token, name, first_scan, proxy)
            elif platform == "xhs":   # 单条笔记
                total_new, author = await self._cw_xhs_note(watch_id, state, aweme_id,
                                                            xsec_token, name, first_scan, proxy)
            elif platform == "kuaishou" and kind == "user":
                total_new, author = await self._cw_ks_user(watch_id, identity, sec_uid,
                                                           name, first_scan)
            elif platform == "kuaishou":   # 单条作品
                total_new, author = await self._cw_ks_video(watch_id, identity, aweme_id,
                                                            name, first_scan)
            elif kind == "user" and mode == "creator":
                total_new, author = await self._cw_creator(watch_id, identity, has_creator,
                                                           name, first_scan)
            elif kind == "user":
                total_new, author = await self._cw_user_public(watch_id, identity, sec_uid,
                                                               name, first_scan)
            else:  # video
                total_new, author = await self._cw_video(watch_id, identity, aweme_id,
                                                         name, first_scan)
        except XhsApiError as e:
            error = e
        except Exception as e:
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else repr(e))
            log.warning("评论监控 %s 失败: %s", watch_id, e)

        with get_session() as s:
            w = s.get(CommentWatch, watch_id)
            if w:
                w.last_scan_at = datetime.utcnow()
                w.last_error = str(error or "")
                if author:
                    if not w.title:
                        w.title = author.get("nickname") or w.title
                    if not w.avatar:
                        ava = (author.get("avatar_thumb") or {}).get("url_list") or []
                        w.avatar = ava[0] if ava else w.avatar
                w.comment_count = len(s.exec(select(CommentRecord.id)
                                             .where(CommentRecord.watch_id == watch_id)).all())
                s.add(w); s.commit()
        return {"ok": not error, "new_comments": total_new, "error": error}

    async def _ingest(self, watch_id, aweme_id, fresh, name, work_desc, first_scan,
                      platform="douyin") -> int:
        """fresh: parse_comment 结果(无 aweme_id)。入库 + 按时间水位线推送。"""
        if not fresh:
            return 0
        with get_session() as s:
            times = s.exec(select(CommentRecord.create_time)
                           .where(CommentRecord.watch_id == watch_id)
                           .where(CommentRecord.aweme_id == aweme_id)).all()
            prev_max = max([t for t in times if t] or [0])
            for c in fresh:
                s.add(CommentRecord(platform=platform, watch_id=watch_id,
                                    aweme_id=aweme_id, **c))
            s.commit()
        newer = [c for c in fresh if c["create_time"] > prev_max]
        if not first_scan and newer:
            await self._notify_comments(name, work_desc, newer)
        return len(fresh)

    def _comment_watch_settings(self, watch_id: int) -> dict:
        cfg = self.cfg.engine
        with get_session() as s:
            watch = s.get(CommentWatch, watch_id)
            return {
                "recent_works": ((watch.recent_works if watch else 0)
                                 or cfg.comment_recent_works),
                "recent_days": ((watch.recent_days if watch else 0)
                                or cfg.comment_recent_days),
                "max_scrolls": ((watch.max_scrolls if watch else 0)
                                or cfg.comment_max_scrolls),
            }

    async def _cw_video(self, watch_id, identity, aweme_id, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)
                               .where(CommentRecord.aweme_id == aweme_id)).all())
        raw, err = await fetch_comments(self.browser, identity, aweme_id, known,
                                        max_scrolls=settings["max_scrolls"],
                                        block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(视频)%s: %s", aweme_id, err)
        fresh = [c for c in (parse_comment(rc) for rc in raw) if c]
        n = await self._ingest(watch_id, aweme_id, fresh, name, name, first_scan)
        return n, None

    async def _cw_user_public(self, watch_id, identity, sec_uid, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        items, author, err = await fetch_videos(self.browser, identity, sec_uid, set(),
                                                max_scrolls=4,
                                                block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(账号)%s: %s", sec_uid, err)
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        works = []
        for it in items:
            aid = str(it.get("aweme_id") or "")
            ct = int(it.get("create_time") or 0)
            if aid and ct >= cutoff:
                works.append((aid, (it.get("desc") or "")))
        works = works[:settings["recent_works"]]
        total = 0
        for aid, desc in works:
            with get_session() as s:
                known = set(s.exec(select(CommentRecord.comment_id)
                                   .where(CommentRecord.watch_id == watch_id)
                                   .where(CommentRecord.aweme_id == aid)).all())
            raw, _e = await fetch_comments(self.browser, identity, aid, known,
                                           max_scrolls=settings["max_scrolls"],
                                           block_media=cfg.block_media_resources)
            fresh = [c for c in (parse_comment(rc) for rc in raw) if c]
            total += await self._ingest(watch_id, aid, fresh, name, desc, first_scan)
        return total, author

    async def _cw_ks_video(self, watch_id, identity, photo_id, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)
                               .where(CommentRecord.aweme_id == photo_id)).all())
        raw, err = await fetch_ks_comments(self.browser, identity, photo_id, known,
                                           max_scrolls=settings["max_scrolls"],
                                           block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(快手作品)%s: %s", photo_id, err)
        fresh = [c for c in (parse_ks_comment(rc) for rc in flatten_ks_comments(raw)) if c]
        n = await self._ingest(watch_id, photo_id, fresh, name, name, first_scan,
                               platform="kuaishou")
        return n, None

    async def _cw_ks_user(self, watch_id, identity, user_id, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        items, author, err = await fetch_ks_videos(self.browser, identity, user_id, set(),
                                                   max_scrolls=4,
                                                   block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(快手账号)%s: %s", user_id, err)
        works = []
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        for feed in items:
            aw = parse_ks_feed(feed)
            if aw and (not aw.create_time or aw.create_time >= cutoff):
                works.append((aw.aweme_id, aw.desc))
                if len(works) >= settings["recent_works"]:
                    break
        total = 0
        for pid, desc in works:
            with get_session() as s:
                known = set(s.exec(select(CommentRecord.comment_id)
                                   .where(CommentRecord.watch_id == watch_id)
                                   .where(CommentRecord.aweme_id == pid)).all())
            raw, _e = await fetch_ks_comments(self.browser, identity, pid, known,
                                              max_scrolls=settings["max_scrolls"],
                                              block_media=cfg.block_media_resources)
            fresh = [c for c in (parse_ks_comment(rc) for rc in flatten_ks_comments(raw)) if c]
            total += await self._ingest(watch_id, pid, fresh, name, desc, first_scan,
                                        platform="kuaishou")
        author_dict = parse_ks_self_user(author) if author else None
        return total, ({"nickname": author_dict["nickname"],
                        "avatar_thumb": {"url_list": [author_dict["avatar"]]}}
                       if author_dict else None)

    async def _cw_creator(self, watch_id, identity, has_creator, name, first_scan):
        if not has_creator:
            log.warning("评论监控 %s 选创作中心,但账号无创作者登录态", watch_id)
            return 0, None
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)).all())
            times = s.exec(select(CommentRecord.create_time)
                           .where(CommentRecord.watch_id == watch_id)).all()
            prev_max = max([t for t in times if t] or [0])
        raw, err = await fetch_creator_comments(self.browser, identity, known,
                                                page_url=cfg.creator_comment_url,
                                                max_scrolls=settings["max_scrolls"],
                                                block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(创作中心): %s", err)
        fresh = [c for c in (parse_creator_comment(rc) for rc in raw) if c]
        if not fresh:
            return 0, None
        with get_session() as s:
            for c in fresh:
                s.add(CommentRecord(watch_id=watch_id, **c))   # c 自带 aweme_id
            s.commit()
        newer = [c for c in fresh if c["create_time"] > prev_max]
        if not first_scan and newer:
            await self._notify_comments(name, "(创作中心)", newer)
        return len(fresh), None

    def _xhs_client(self, state: str, proxy: str = ""):
        cookie_str = cookie_str_from_state(state)
        if not has_a1(cookie_str):
            return None
        return XhsApiClient(cookie_str, self.cfg.engine.user_agent,
                            timeout=self.cfg.engine.request_timeout_seconds, proxy=proxy)

    async def _xhs_fetch_comments(self, client, note_id, xsec_token, known) -> list:
        try:
            d = await client.note_comments(note_id, xsec_token=xsec_token)
            raw = d.get("comments") or []
        except XhsApiError:
            raise
        except Exception as e:
            category, _signal = classify_platform_error(e)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                raise
            log.info("评论监控(小红书)%s: %s", note_id, e)
            return []
        fresh = [c for c in (parse_xhs_comment(rc) for rc in flatten_xhs_comments(raw)) if c]
        return [c for c in fresh if c["comment_id"] not in known]

    async def _cw_xhs_note(self, watch_id, state, note_id, xsec_token, name, first_scan,
                           proxy=""):
        client = self._xhs_client(state, proxy)
        if client is None:
            return 0, None
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)
                               .where(CommentRecord.aweme_id == note_id)).all())
        fresh = await self._xhs_fetch_comments(client, note_id, xsec_token, known)
        n = await self._ingest(watch_id, note_id, fresh, name, name, first_scan,
                               platform="xhs")
        return n, None

    async def _cw_xhs_creator(self, watch_id, state, user_id, xsec_token, name, first_scan,
                              proxy=""):
        settings = self._comment_watch_settings(watch_id)
        client = self._xhs_client(state, proxy)
        if client is None:
            return 0, None
        try:
            d = await client.notes_by_creator(user_id, xsec_token=xsec_token)
            briefs_raw = d.get("notes") or []
            author = await client.user_info(user_id)
        except XhsApiError:
            raise
        except Exception as e:
            category, _signal = classify_platform_error(e)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                raise
            log.info("评论监控(小红书创作者)%s: %s", user_id, e)
            briefs_raw, author = [], None
        briefs = [b for b in (parse_note_brief(r) for r in briefs_raw) if b]
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        briefs = [b for b in briefs
                  if not b.get("create_time") or b["create_time"] >= cutoff]
        briefs = briefs[:settings["recent_works"]]
        total = 0
        for b in briefs:
            nid = b["note_id"]
            with get_session() as s:
                known = set(s.exec(select(CommentRecord.comment_id)
                                   .where(CommentRecord.watch_id == watch_id)
                                   .where(CommentRecord.aweme_id == nid)).all())
            fresh = await self._xhs_fetch_comments(client, nid, b.get("xsec_token", ""), known)
            total += await self._ingest(watch_id, nid, fresh, name, b.get("title", ""),
                                        first_scan, platform="xhs")
        author_dict = parse_xhs_self_user(author) if author else None
        return total, ({"nickname": author_dict["nickname"],
                        "avatar_thumb": {"url_list": [author_dict["avatar"]]}}
                       if author_dict else None)

    async def _notify_comments(self, target_name: str, work_desc: str, comments: list):
        with get_session() as s:
            chans = s.exec(select(NotificationChannel)
                           .where(NotificationChannel.enabled == True)).all()  # noqa: E712
            channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
        if not channels:
            return
        title = f"评论监控 · {target_name} 有 {len(comments)} 条新评论"
        head = (work_desc or "")[:20]
        lines = [f"作品:{head}"]
        for c in comments[:6]:
            lines.append(f"· {c['user_nickname']}: {c['text'][:40]}")
        if len(comments) > 6:
            lines.append(f"… 等共 {len(comments)} 条")
        try:
            await notify_all(channels, title, "\n".join(lines))
        except Exception as e:
            log.warning("评论通知失败: %s", e)

    async def _notify_danmaku(self, target_name: str, work_desc: str,
                              danmakus: list):
        with get_session() as s:
            chans = s.exec(select(NotificationChannel).where(
                NotificationChannel.enabled == True)).all()  # noqa: E712
            channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
        if not channels:
            return
        title = f"弹幕监控 · {target_name} 有 {len(danmakus)} 条新弹幕"
        lines = [f"作品:{(work_desc or '')[:20]}"]
        for item in danmakus[:6]:
            point = int(item.get("video_time_ms") or 0) // 1000
            stamp = f"{point // 60}:{point % 60:02d}"
            lines.append(f"· [{stamp}] {item.get('user_nickname') or '用户'}: "
                         f"{(item.get('text') or '')[:40]}")
        if len(danmakus) > 6:
            lines.append(f"… 等共 {len(danmakus)} 条")
        try:
            await notify_all(channels, title, "\n".join(lines))
        except Exception as e:
            log.warning("弹幕通知失败: %s", e)
