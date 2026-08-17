"""监控目标扫描:抖音/快手/小红书作品发现、账号巡检、健康度、下载重试。

MonitorEngine 的 Mixin(2026-08-17 从 monitor.py 拆出):方法仍属同一个引擎
实例,self 状态共享不变;拆分只为按职责分文件。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from sqlmodel import select
from application.browser import (fetch_videos, fetch_self_profile, fetch_ks_videos, fetch_ks_self_profile, fetch_channels_self_profile, fetch_account_works)
from moss.common.db import get_session
from application.douyin import parse_self_user
from application.douyin.extract import Aweme, MediaItem
from application.xhs import (parse_note_brief, parse_note_detail, parse_self_user as parse_xhs_self_user, XhsApiClient, XhsApiError, cookie_str_from_state, has_a1, creator_check)
from application.kuaishou import parse_ks_feed, parse_self_user as parse_ks_self_user
from application.channels import parse_self_user as parse_channels_self_user
from application.registry import ARTICLE_KEYS
from moss.model import (ContentRecord, DouyinAccount, MonitorTarget, NotificationChannel, AccountWork, AccountStatSnapshot)
from moss.common.notifier import notify_all
from moss.common.netfp import probe_ip_region
from moss.core.risk import classify_platform_error, OperationKind, RiskCategory
from moss.core.settings import get_setting

from application.engine._helpers import MAX_AUTO_RETRY, _TZ_COUNTRY, _douyin_scan_since, _loads, _round_robin_by_account, _select_douyin_awemes, log


class ScanOps:
    # 本账号作品健康监控:视为「异常/受限」的状态关键词(命中即告警)
    _BAD_STATUS = ("违规", "删除", "下架", "不适宜", "限流", "私密", "仅自己", "审核不")

    def _stamp_active(self, account_id) -> None:
        """记录账号「刚被成功摸活」的时刻。任何一次成功的网络/浏览器动作都算活跃,
        闲置保活据此跳过近期已活跃的账号,避免重复请求、减少风控暴露面。"""
        if not account_id:
            return
        with get_session() as s:
            a = s.get(DouyinAccount, account_id)
            if a:
                a.last_active_at = datetime.utcnow()
                s.add(a); s.commit()

    def _keepalive_due(self, last_active_at) -> bool:
        """闲置判定:从未活跃、或距上次活跃超过 idle_keepalive_hours(带 ±jitter 错峰)才需保活。
        idle_keepalive_hours<=0 时退回旧行为(每轮都摸)。"""
        hours = self.cfg.engine.idle_keepalive_hours
        if hours <= 0 or last_active_at is None:
            return True
        jitter = max(0.0, self.cfg.engine.scan_jitter)
        factor = 1.0 + random.uniform(-jitter, jitter) if jitter else 1.0
        return (datetime.utcnow() - last_active_at).total_seconds() >= hours * 3600 * factor

    async def _verify_proxy_region(self, account_id, proxy: str, timezone_id: str) -> None:
        """探测代理出口国家,与账号时区期望不一致时告警(best-effort,只记日志)。
        同一账号+代理只探测一次(缓存),失败静默 —— IP 在境外却时区东八区是强关联信号。"""
        if not self.cfg.engine.verify_proxy_region or not proxy:
            return
        if self._geo_checked.get(account_id) == proxy:
            return
        self._geo_checked[account_id] = proxy
        expected = _TZ_COUNTRY.get(timezone_id or "")
        if not expected:
            return
        geo = await probe_ip_region(proxy)
        if not geo:
            return
        # 把代理出口的真实经纬度写回账号 —— geolocation 伪造坐标据此对齐真实出口地,
        # 避免 navigator.geolocation(城市池兜底)与代理 IP 归属地对不上。
        lat, lon = geo.get("lat") or 0.0, geo.get("lon") or 0.0
        if lat and lon:
            try:
                with get_session() as s:
                    acc = s.get(DouyinAccount, account_id)
                    if acc and (round(acc.geo_lat, 3) != round(lat, 3)
                                or round(acc.geo_lon, 3) != round(lon, 3)):
                        acc.geo_lat, acc.geo_lon = lat, lon
                        s.add(acc)
                        s.commit()
            except Exception as e:
                log.debug(f"账号出口坐标回写失败(不影响巡检): {e!r}")
        if not geo.get("country"):
            return
        if geo["country"] != expected:
            log.warning("账号 %s 代理出口国家 %s 与时区 %s(期望 %s)不一致,IP=%s"
                        " —— 关联/风控风险,建议换地区一致的长效代理或改账号时区",
                        account_id, geo["country"], timezone_id, expected, geo.get("ip"))

    async def _check_accounts(self):
        interval = self.cfg.engine.account_check_interval_seconds
        if interval <= 0:
            return
        if time.time() - self._last_acct_check < interval:
            return
        self._last_acct_check = time.time()
        with get_session() as s:
            accs = []
            for a in s.exec(select(DouyinAccount)).all():
                if not (a.storage_state or a.creator_storage_state):
                    continue
                if a.platform in ARTICLE_KEYS:
                    # 图文平台是纯 HTTP 死 Cookie,下面的浏览器体检全是视频平台
                    # 语义——拿百度/微信 Cookie 逛抖音必成游客态,会把账号误标
                    # invalid。判活走 /api/accounts/{id}/check-article-login。
                    continue
                if a.status == "invalid":
                    continue                       # 已失效:摸也救不活,等用户重登,别白发请求
                if not self._keepalive_due(a.last_active_at):
                    continue                       # 近期已被监控/发布/上轮保活摸过,跳过
                accs.append((a.id, a.platform, a.storage_state, a.creator_storage_state,
                             a.proxy or "", self.browser.identity_for(a)))
        for aid, platform, state, creator_state, proxy, identity in accs:
            decision = self.risk.preflight(aid, OperationKind.READ_LIGHT)
            if not decision.allowed:
                continue
            try:
                async with self._operation_guard(aid, OperationKind.READ_LIGHT):
                    if not self.risk.preflight(
                            aid, OperationKind.READ_LIGHT).allowed:
                        continue
                    await self._verify_proxy_region(aid, proxy, identity.timezone_id)
                    if platform == "xhs" and creator_state:
                        # 创作者号:用创作平台接口校验(www 的 user/me 对创作态会误判)
                        chk = await creator_check(creator_state, proxy=proxy)
                        if chk is None:
                            continue                 # 不确定,保持原状态
                        u, err = ({"ok": 1}, "") if chk else ({}, "logged_out")
                    elif platform == "xhs":
                        client = self._xhs_client(state, proxy)
                        if client is None:
                            u, err = {}, "logged_out"
                        else:
                            try:
                                d = await client.self_info()
                                u, err = (d, "") if (d and not d.get("guest")) else ({}, "logged_out")
                            except XhsApiError as exc:
                                if exc.category == "auth":
                                    u, err = {}, "logged_out"
                                else:
                                    self.risk.record_failure(
                                        aid, OperationKind.READ_LIGHT, exc)
                                    continue
                    elif platform == "kuaishou":
                        u, err = await fetch_ks_self_profile(self.browser, identity)
                    elif platform == "shipinhao":
                        u, err = await fetch_channels_self_profile(self.browser, identity)
                    else:
                        u, err = await fetch_self_profile(self.browser, identity)
                    if u:
                        self.risk.record_success(aid, OperationKind.READ_LIGHT)
                    elif err:
                        self.risk.record_failure(aid, OperationKind.READ_LIGHT, err)
            except Exception as exc:
                self.risk.record_failure(aid, OperationKind.READ_LIGHT, exc)
                continue
            with get_session() as s:
                a = s.get(DouyinAccount, aid)
                if not a:
                    continue
                if u:
                    if platform == "xhs":
                        p = parse_xhs_self_user(u)
                    elif platform == "kuaishou":
                        p = parse_ks_self_user(u)
                    elif platform == "shipinhao":
                        p = parse_channels_self_user(u)
                    else:
                        p = parse_self_user(u)
                    a.status = "active"
                    a.last_active_at = datetime.utcnow()   # 保活成功:重置闲置计时
                    if p.get("nickname"):
                        a.nickname = p["nickname"]
                    a.sec_uid = p.get("sec_uid") or a.sec_uid
                    a.douyin_id = p.get("douyin_id") or a.douyin_id
                    a.avatar = p.get("avatar") or a.avatar
                    a.follower_count = p.get("follower_count") or a.follower_count
                    a.aweme_count = p.get("aweme_count") or a.aweme_count
                    got_profile = True
                elif err == "logged_out":
                    a.status = "invalid"
                    log.warning("账号 %s(%s)登录态失效", aid, a.nickname)
                    got_profile = False
                else:
                    got_profile = False
                s.add(a); s.commit()
            # 体检成功即记一条粉丝/作品数快照(B4 趋势;不依赖作品健康开关也能出粉丝曲线)
            if got_profile and self.cfg.engine.work_health_stat_snapshots:
                try:
                    self._write_stat_snapshot(aid, platform, [])
                except Exception:
                    pass

    async def _check_work_health(self):
        """定期同步本账号作品,检测「持续0播 / 违规下架」并推送;顺带写每日数据快照。
        默认关闭(work_health_enabled)。较重(每账号开一次浏览器抓自己作品),故独立节流。"""
        if not self.cfg.engine.work_health_enabled:
            return
        now = time.time()
        if now - getattr(self, "_last_work_health", 0.0) < \
                self.cfg.engine.work_health_interval_seconds:
            return
        self._last_work_health = now
        with get_session() as s:
            accs = [(a.id, a.platform, a.sec_uid or "", self.browser.identity_for(a))
                    for a in s.exec(select(DouyinAccount)).all()
                    if a.status != "invalid" and a.platform not in ARTICLE_KEYS
                    and (a.storage_state or a.creator_storage_state)]
        for aid, platform, uid, identity in accs:
            decision = self.risk.preflight(aid, OperationKind.READ_HEAVY)
            if not decision.allowed:
                continue
            try:
                async with self._operation_guard(aid, OperationKind.READ_HEAVY):
                    if not self.risk.preflight(
                            aid, OperationKind.READ_HEAVY).allowed:
                        continue
                    items, err = await fetch_account_works(self.browser, identity,
                                                           platform, uid)
                    if err:
                        self.risk.record_failure(aid, OperationKind.READ_HEAVY, err)
                    else:
                        self.risk.record_success(aid, OperationKind.READ_HEAVY)
            except Exception as e:
                self.risk.record_failure(aid, OperationKind.READ_HEAVY, e)
                log.warning("作品健康:账号 %s 抓取失败 %s", aid, e)
                continue
            if not items:
                continue
            self._stamp_active(aid)
            try:
                await self._eval_work_health(aid, platform, items)
            except Exception as e:
                log.warning("作品健康:账号 %s 评估失败 %s", aid, e)

    async def _eval_work_health(self, account_id, platform, items):
        """upsert 本账号作品 + 判定 0播/违规告警 + 写数据快照。"""
        now = datetime.utcnow()
        zero_hours = self.cfg.engine.work_health_zero_play_hours
        cutoff = time.time() - self.cfg.engine.work_health_recent_days * 86400
        # 该平台是否真的暴露播放量:有任一作品 play>0 才启用「0播」判定,避免对不报播放量的
        # 平台(web 抖音等)误报。
        play_reliable = any((w.get("play_count") or 0) > 0 for w in items)
        alerts = []
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            nick = (acc.nickname if acc else "") or f"账号{account_id}"
            for w in items:
                rec = s.exec(select(AccountWork).where(
                    AccountWork.account_id == account_id,
                    AccountWork.item_id == w["item_id"])).first()
                if rec:
                    for k, v in w.items():
                        setattr(rec, k, v)
                    rec.fetched_at = now
                else:
                    rec = AccountWork(platform=platform, account_id=account_id,
                                      fetched_at=now, **w)
                    s.add(rec); s.flush()
                ct = rec.create_time or 0
                if ct and ct < cutoff:
                    s.add(rec); continue          # 太老,不体检
                title = (rec.desc or rec.item_id or "")[:20]
                st = rec.status or ""
                if st and any(k in st for k in self._BAD_STATUS) and rec.status_alerted != st:
                    alerts.append(("⚠️ 视频号/作品状态异常" if platform == "shipinhao"
                                   else "⚠️ 作品状态异常",
                                   f"{nick}:「{title}」当前状态「{st}」"))
                    rec.status_alerted = st
                if play_reliable and ct:
                    age_h = (time.time() - ct) / 3600
                    if (age_h >= zero_hours and (rec.play_count or 0) == 0
                            and not rec.zero_play_alerted):
                        alerts.append(("⚠️ 作品持续0播",
                                       f"{nick}:「{title}」发布 {age_h:.0f} 小时仍 0 播放,疑似限流"))
                        rec.zero_play_alerted = True
                s.add(rec)
            s.commit()
        if self.cfg.engine.work_health_stat_snapshots:
            self._write_stat_snapshot(account_id, platform, items)
        if alerts:
            try:
                with get_session() as s:
                    chans = s.exec(select(NotificationChannel)
                                   .where(NotificationChannel.enabled == True)).all()  # noqa: E712
                    channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
                for title, body in alerts:
                    if channels:
                        await notify_all(channels, title, body)
                    log.info("作品健康告警: %s | %s", title, body)
            except Exception as e:
                log.warning(f"作品健康告警通知发送失败: {e!r}")

    def _write_stat_snapshot(self, account_id, platform, items):
        """每账号每天一行数据快照(粉丝/作品/互动合计),供「数据」趋势视图。"""
        day = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")  # 东八区日期
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            snap = s.exec(select(AccountStatSnapshot).where(
                AccountStatSnapshot.account_id == account_id,
                AccountStatSnapshot.date == day)).first()
            if not snap:
                snap = AccountStatSnapshot(platform=platform, account_id=account_id, date=day)
            snap.follower_count = (acc.follower_count if acc else 0) or snap.follower_count
            snap.aweme_count = (acc.aweme_count if acc else 0) or snap.aweme_count or len(items)
            # 只有带作品列表(作品健康那趟)才更新互动合计;粉丝-only 快照不清零已有合计
            if items:
                snap.total_like = sum((w.get("like_count") or 0) for w in items)
                snap.total_comment = sum((w.get("comment_count") or 0) for w in items)
                snap.total_play = sum((w.get("play_count") or 0) for w in items)
            s.add(snap); s.commit()

    def _due(self, last_scan_at, interval_seconds) -> bool:
        """到点判断,叠加 ±jitter 随机,避免所有目标整点齐发(机器矩阵特征)。"""
        if last_scan_at is None:
            return True
        jitter = max(0.0, self.cfg.engine.scan_jitter)
        factor = 1.0 + random.uniform(-jitter, jitter) if jitter else 1.0
        return (datetime.utcnow() - last_scan_at).total_seconds() >= interval_seconds * factor

    async def _scan_once(self):
        due: list[tuple[int, int | None]] = []
        with get_session() as s:
            targets = s.exec(select(MonitorTarget).where(MonitorTarget.enabled == True)).all()  # noqa: E712
            for t in targets:
                if self._due(t.last_scan_at, t.interval_seconds):
                    due.append((t.id, t.account_id))
        if due:
            ordered = _round_robin_by_account(due)
            await asyncio.gather(*(self.scan_target(tid) for tid, _ in ordered))

    async def scan_target(self, target_id: int) -> dict:
        if target_id in self._inflight:
            return {"ok": True, "new": 0, "skipped": "正在抓取中"}
        self._inflight.add(target_id)
        try:
            with get_session() as s:
                t = s.get(MonitorTarget, target_id)
                account_id = t.account_id if t else None
            decision = self.risk.preflight(account_id, OperationKind.READ_LIGHT)
            if not decision.allowed:
                return {
                    "ok": True,
                    "new": 0,
                    "skipped": True,
                    "reason": decision.reason,
                    "next_allowed_at": (
                        decision.next_allowed_at.isoformat()
                        if decision.next_allowed_at else None),
                }
            async with self._operation_guard(
                    account_id, OperationKind.READ_LIGHT,
                    fallback_key=f"tgt:{target_id}"):
                decision = self.risk.preflight(account_id, OperationKind.READ_LIGHT)
                if not decision.allowed:
                    return {
                        "ok": True, "new": 0, "skipped": True,
                        "reason": decision.reason,
                        "next_allowed_at": (
                            decision.next_allowed_at.isoformat()
                            if decision.next_allowed_at else None),
                    }
                res = await self._scan_target_locked(target_id)
                error = res.get("error")
                if account_id and res.get("ok") and not res.get("skipped"):
                    self.risk.record_success(account_id, OperationKind.READ_LIGHT)
                    self._stamp_active(account_id)
                elif account_id and error:
                    self.risk.record_failure(
                        account_id, OperationKind.READ_LIGHT, error)
                if isinstance(error, BaseException):
                    res = dict(res)
                    res["error"] = str(error)
            # 用该账号成功抓取过=登录态被有效使用,顺带续期,免得再被闲置保活重复摸
            return res
        finally:
            self._inflight.discard(target_id)

    async def _scan_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            t0 = s.get(MonitorTarget, target_id)
            if not t0:
                return {"ok": False, "error": "target not found"}
            platform = t0.platform
        if platform == "xhs":
            return await self._scan_xhs_target_locked(target_id)
        if platform == "kuaishou":
            return await self._scan_ks_target_locked(target_id)
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None   # 首扫建立时间基线，可按目标配置回填历史
            if not target.account_id:
                return self._mark_target_skip(
                    target_id, "抖音作品监控必须绑定已登录账号,匿名主页可能返回陈旧或残缺作品")
            acc = s.get(DouyinAccount, target.account_id)
            if not acc or acc.platform != "douyin" or acc.status != "active":
                return self._mark_target_skip(
                    target_id, "绑定的抖音账号不存在或登录态已失效,请重新绑定/登录")
            if self._proxy_bad(acc):
                return self._mark_target_skip(
                    target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
            identity, proxy = self._identity_proxy(acc)
            # 只取 aweme_id 列,避免把整行作品都加载进内存
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            known_create_times = list(s.exec(
                select(ContentRecord.create_time)
                .where(ContentRecord.target_id == target_id)).all())
            sec_uid = target.sec_uid
            # 有效下载目录:目标自定义 > 全局默认 > 配置兜底
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            # 有效画质:目标自定义 > 全局默认 > highest
            quality = target.video_quality or get_setting("video_quality", "highest")
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"

        items, author, error = await fetch_videos(
            self.browser, identity, sec_uid, known,
            block_media=self.cfg.engine.block_media_resources,
            # 默认只监控订阅后的作品；显式首次回填时允许继续向历史翻页。
            stop_before=(0 if first_scan and backfill_count != 0 else scan_since))

        new_records = []
        selected = _select_douyin_awemes(
            items, quality, first_scan, scan_since, backfill_count)
        for aw in selected:
            should_download = auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([{"url": m.url, "kind": m.kind, "ext": m.ext,
                                      "index": m.index} for m in aw.medias])
            rec = ContentRecord(
                target_id=target_id, aweme_id=aw.aweme_id, desc=aw.desc,
                media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json,
                download_status="pending" if should_download else "skipped",
            )
            new_records.append((rec, aw, should_download))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = error
                if author:  # 首次抓到时补全昵称/头像
                    if not t.nickname:
                        t.nickname = author.get("nickname", "") or t.nickname
                    if not t.avatar:
                        ava = (author.get("avatar_thumb") or {}).get("url_list") or []
                        t.avatar = ava[0] if ava else t.avatar
                s.add(t)
                target_name = t.nickname or t.sec_uid[:12]
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not first_scan:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error}

    async def _scan_ks_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            identity = self.browser.anon_identity()
            proxy = ""
            if target.account_id:
                acc = s.get(DouyinAccount, target.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        return self._mark_target_skip(
                            target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
                    identity, proxy = self._identity_proxy(acc)
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            user_id = target.sec_uid
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            quality = target.video_quality or get_setting("video_quality", "highest")
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"

        items, author, error = await fetch_ks_videos(
            self.browser, identity, user_id, known,
            block_media=self.cfg.engine.block_media_resources)

        new_records = []
        seen = set()
        for item in items:
            aw = parse_ks_feed(item, quality)
            if not aw or aw.aweme_id in seen:
                continue
            seen.add(aw.aweme_id)
            should_download = auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([{"url": m.url, "kind": m.kind, "ext": m.ext,
                                      "index": m.index} for m in aw.medias])
            rec = ContentRecord(
                platform="kuaishou", target_id=target_id, aweme_id=aw.aweme_id,
                desc=aw.desc, media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json,
                download_status="pending" if should_download else "skipped",
            )
            new_records.append((rec, aw, should_download))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = error
                if author:   # author 为 userProfile 形状
                    p = parse_ks_self_user(author)
                    if not t.nickname:
                        t.nickname = p.get("nickname") or t.nickname
                    if not t.avatar:
                        t.avatar = p.get("avatar") or t.avatar
                s.add(t)
                target_name = t.nickname or (user_id[:12] if user_id else "kuaishou")
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not first_scan:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error}

    def _mark_target_skip(self, target_id: int, msg: str) -> dict:
        """把跳过原因写到目标 last_error,并推进 last_scan_at(避免下轮立刻重试)。"""
        with get_session() as s:
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = msg
                s.add(t); s.commit()
        return {"ok": False, "new": 0, "error": msg, "skipped": True}

    async def _scan_xhs_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            kind = target.target_kind
            user_id, keyword = target.sec_uid, target.keyword
            xsec_token = target.xsec_token or ""
            state = ""
            proxy = ""
            if target.account_id:
                acc = s.get(DouyinAccount, target.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        return self._mark_target_skip(
                            target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
                    state = acc.storage_state or ""
                    proxy = acc.proxy or ""
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"

        # 小红书签名直连需要登录态里的 a1 / web_session 等 Cookie
        cookie_str = cookie_str_from_state(state)
        if not state or not has_a1(cookie_str):
            msg = "小红书监控需要绑定一个已登录的小红书账号(登录态缺少 a1,请重新扫码登录)"
            with get_session() as s:
                t = s.get(MonitorTarget, target_id)
                if t:
                    t.last_scan_at = datetime.utcnow()
                    t.last_error = msg
                    s.add(t); s.commit()
            return {"ok": False, "new": 0, "error": msg}

        client = XhsApiClient(cookie_str, self.cfg.engine.user_agent,
                              timeout=self.cfg.engine.request_timeout_seconds, proxy=proxy)
        error = ""
        author = None
        briefs_raw: list = []
        try:
            if kind == "keyword":
                briefs_raw = await client.search_notes(keyword)
            else:
                d = await client.notes_by_creator(user_id, xsec_token=xsec_token)
                briefs_raw = d.get("notes") or []
                try:
                    author = await client.user_info(user_id)
                except XhsApiError as e:
                    error = e
                    author = None
                except Exception as exc:
                    category, _signal = classify_platform_error(exc)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        raise
                    author = None
        except XhsApiError as e:
            error = e
        except Exception as e:
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else f"小红书接口请求失败: {e!r}")

        # 逐条新笔记调 feed 接口拿完整媒体直链(单轮限量,避免请求过多被风控)
        new_records = []
        seen = set()
        MAX_PER_SCAN = 12
        for raw in briefs_raw:
            if error and classify_platform_error(error)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                break
            brief = parse_note_brief(raw)
            if not brief or brief["note_id"] in seen or brief["note_id"] in known:
                continue
            seen.add(brief["note_id"])
            if len(new_records) >= MAX_PER_SCAN:
                break
            if seen and len(seen) > 1:
                await asyncio.sleep(0.6)   # 给 feed 接口留间隔,降低被风控/限流的概率
            note_tok = brief.get("xsec_token", "")
            derr = ""
            card = {}
            try:
                card = await client.note_detail(
                    brief["note_id"], xsec_token=note_tok,
                    xsec_source="pc_search" if kind == "keyword" else "pc_feed")
            except XhsApiError as e:
                error = e
                break
            except Exception as e:
                category, _signal = classify_platform_error(e)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    error = e
                    break
                derr = str(e)
            aw = parse_note_detail(card or {}, brief) if card else None
            if not aw:
                # 详情抓取失败也建一条 failed 记录,保留 xsec_token 便于重试
                aw = Aweme(aweme_id=brief["note_id"], desc=brief.get("title", ""),
                           create_time=0, author_name="", media_type="images")
                aw.platform = "xhs"
                aw.cover = brief.get("cover", "")
            should_download = bool(aw.medias) and auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([{"url": m.url, "kind": m.kind, "ext": m.ext,
                                      "index": m.index} for m in aw.medias])
            rec = ContentRecord(
                platform="xhs", target_id=target_id, aweme_id=aw.aweme_id, desc=aw.desc,
                media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json, xsec_token=note_tok,
                download_status=("pending" if should_download
                                 else ("skipped" if aw.medias else "failed")),
                error="" if aw.medias else (derr or "未取到媒体直链"),
            )
            new_records.append((rec, aw, should_download))

        log.debug(f"[xhs_scan] kind={kind} key={keyword or user_id} briefs={len(briefs_raw)} "
              f"new_records={len(new_records)} "
              f"with_media={sum(1 for _, a, _ in new_records if a.medias)} error={error!r}")

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = str(error or "")
                if author:  # 创作者资料(otherinfo)
                    p = parse_xhs_self_user(author)
                    if not t.nickname:
                        t.nickname = p.get("nickname") or t.nickname
                    if not t.avatar:
                        t.avatar = p.get("avatar") or t.avatar
                s.add(t)
                target_name = t.nickname or (("#" + keyword) if kind == "keyword"
                                             else (user_id[:12] if user_id else "xhs"))
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not first_scan:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error}

    async def _notify_new(self, target_name: str, awemes: list):
        """有新作品时推送到所有启用的通知渠道。"""
        with get_session() as s:
            chans = s.exec(select(NotificationChannel)
                           .where(NotificationChannel.enabled == True)).all()  # noqa: E712
            channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
        if not channels:
            return
        title = f"作品监控 · {target_name} 新增 {len(awemes)} 个作品"
        lines = []
        for aw in awemes[:6]:
            tag = "图集" if aw.media_type == "images" else "视频"
            lines.append(f"· [{tag}] {(aw.desc or aw.aweme_id)[:30]}")
        if len(awemes) > 6:
            lines.append(f"… 等共 {len(awemes)} 个")
        try:
            await notify_all(channels, title, "\n".join(lines))
        except Exception as e:
            log.warning("通知发送失败: %s", e)

    async def _download(self, record_id: int, aweme, base_dir: str = "", proxy: str = ""):
        async with self._sem:
            with get_session() as s:
                rec = s.get(ContentRecord, record_id)
                if rec:
                    rec.download_status = "downloading"
                    s.add(rec); s.commit()
            ok, path, err = await self.downloader.download_aweme(
                aweme, base_dir, self._dl_proxy(proxy))
            with get_session() as s:
                rec = s.get(ContentRecord, record_id)
                if rec:
                    rec.download_status = "done" if ok else "failed"
                    rec.local_path = path
                    rec.error = err
                    s.add(rec); s.commit()

    def _rebuild_aweme(self, rec: ContentRecord, author_name: str) -> Aweme:
        aw = Aweme(aweme_id=rec.aweme_id, desc=rec.desc, create_time=rec.create_time,
                   author_name=author_name, media_type=rec.media_type)
        aw.platform = rec.platform or "douyin"
        for m in _loads(rec.media_json) if rec.media_json else []:
            aw.medias.append(MediaItem(url=m["url"], kind=m.get("kind", "video"),
                                       ext=m.get("ext", "mp4"), index=m.get("index", 0)))
        return aw

    async def retry_download(self, record_id: int) -> dict:
        """重新下载某条作品(用入库时存下的媒体直链;直链可能过期则需重抓目标)。
        小红书:若当初连详情都没拿到(无媒体快照),这里会用 xsec_token 重新拉一次详情。"""
        with get_session() as s:
            rec = s.get(ContentRecord, record_id)
            if not rec:
                return {"ok": False, "error": "记录不存在"}
            t = s.get(MonitorTarget, rec.target_id)
            base_dir = (t.download_dir if t else "") or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            author_name = (t.nickname if t else "") or ""
            platform = rec.platform or "douyin"
            note_id = rec.aweme_id
            note_tok = rec.xsec_token or ""
            kind = (t.target_kind if t else "creator")
            account_id = t.account_id if t else None
            acc_state = ""
            acc_proxy = ""
            if t and t.account_id:
                acc = s.get(DouyinAccount, t.account_id)
                if acc:
                    acc_state = acc.storage_state or ""
                    acc_proxy = acc.proxy or ""
            media_json = rec.media_json
            aw = self._rebuild_aweme(rec, author_name)
            needs_xhs_refetch = platform == "xhs" and (
                not media_json or not aw.medias)
            rec.download_status = "downloading"
            if not needs_xhs_refetch:
                rec.retry_count = (rec.retry_count or 0) + 1
            s.add(rec); s.commit()

        # 小红书:无媒体快照时,重新拉详情补齐媒体直链
        if platform == "xhs" and (not media_json or not aw.medias):
            client = self._xhs_client(acc_state, acc_proxy)
            derr = "" if client else "账号登录态缺少 a1,请重新扫码登录"
            card = {}
            if client:
                async def _refetch_note_detail():
                    try:
                        detail = await client.note_detail(
                            note_id, xsec_token=note_tok,
                            xsec_source=("pc_search" if kind == "keyword"
                                         else "pc_feed"))
                        return detail, ""
                    except Exception as exc:
                        return {}, str(exc)

                card, derr = await self.guarded_read_pair(
                    account_id, OperationKind.READ_HEAVY,
                    f"retry-download:{record_id}", _refetch_note_detail,
                    empty_result={})
                if not str(derr or "").startswith("risk_deferred:"):
                    with get_session() as s:
                        rec = s.get(ContentRecord, record_id)
                        if rec:
                            rec.retry_count = (rec.retry_count or 0) + 1
                            s.add(rec)
                            s.commit()
            aw2 = parse_note_detail(card or {}, {"note_id": note_id}) if card else None
            if aw2 and aw2.medias:
                aw = aw2
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.media_type = aw.media_type
                        rec.create_time = aw.create_time or rec.create_time
                        rec.like_count = aw.like_count or rec.like_count
                        rec.cover_url = aw.cover or rec.cover_url
                        rec.media_json = json.dumps([{"url": m.url, "kind": m.kind,
                                                      "ext": m.ext, "index": m.index}
                                                     for m in aw.medias])
                        s.add(rec); s.commit()
            else:
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.download_status = "failed"
                        rec.error = derr or "重拉详情仍无媒体(笔记可能已删/私密)"
                        s.add(rec); s.commit()
                return {"ok": False, "error": derr or "重拉详情仍无媒体"}
        elif not media_json or not aw.medias:
            with get_session() as s:
                rec = s.get(ContentRecord, record_id)
                if rec:
                    rec.download_status = "failed"
                    rec.error = "无媒体直链快照,请对该目标重新抓取"
                    s.add(rec); s.commit()
            return {"ok": False, "error": "无媒体直链快照"}

        ok, path, err = await self.downloader.download_aweme(
            aw, base_dir, self._dl_proxy(acc_proxy))
        with get_session() as s:
            rec = s.get(ContentRecord, record_id)
            if rec:
                rec.download_status = "done" if ok else "failed"
                rec.local_path = path
                rec.error = err
                s.add(rec); s.commit()
        return {"ok": ok, "error": err}

    async def _retry_failed(self):
        """自动重试失败且未超过上限的作品。"""
        with get_session() as s:
            ids = list(s.exec(
                select(ContentRecord.id)
                .where(ContentRecord.download_status == "failed")
                .where(ContentRecord.retry_count < MAX_AUTO_RETRY)).all())
        for rid in ids:
            await self.retry_download(rid)
