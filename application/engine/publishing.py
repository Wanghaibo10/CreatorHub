"""发布任务:创作平台发布(视频+图文)与跨平台转发。

MonitorEngine 的 Mixin(2026-08-17 从 monitor.py 拆出):方法仍属同一个引擎
实例,self 状态共享不变;拆分只为按职责分文件。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from sqlmodel import select
from moss.common.db import get_session
from application.douyin import publish_douyin, publish_via_http
from application.xhs import publish_xhs
from application.kuaishou import publish_kuaishou
from application.channels import publish_channels
from application.baijiahao import publish_baijiahao
from application.toutiao import publish_toutiao
from application.wechat_mp import publish_wechat_mp
from application.registry import ARTICLE_KEYS, spec as platform_spec
from moss.model import ContentRecord, DouyinAccount, NotificationChannel, PublishTask
from moss.common.notifier import notify_all
from moss.core.risk import OperationKind, RiskCategory

from application.engine._helpers import _loads, _loads_list, log


class PublishOps:
    def _content_files(self, rec: ContentRecord) -> list:
        """收集一条作品记录在本地的媒体文件路径。"""
        if not rec.local_path:
            return []
        p = Path(rec.local_path)
        if p.is_file():
            return [str(p)]
        folder = p if p.is_dir() else p.parent
        if not folder.exists():
            return []
        # 文件名形如 {aweme_id}_{title}_{index}.{ext};按末尾数字序号排(而非字典序,
        # 否则 10 张以上会 _10 排到 _2 前面 —— 图集顺序错乱、封面选错)。
        def _idx_key(f: Path):
            tail = f.stem.rsplit("_", 1)[-1]
            return (0, int(tail)) if tail.isdigit() else (1, f.name)
        cands = [f for f in folder.glob(f"{rec.aweme_id}_*")
                 if f.is_file() and not f.name.endswith(".part")]
        return [str(f) for f in sorted(cands, key=_idx_key)]

    def create_relay_publish(self, content_id: int, account_id: int,
                             target_platform: str = "xhs",
                             title: Optional[str] = None, desc: Optional[str] = None,
                             topics: Optional[str] = None,
                             visibility: str = "public", allow_save: bool = True,
                             media_order: Optional[list] = None
                             ) -> Optional[int]:
        """从已下载作品创建发往目标平台(小红书/抖音/视频号)的发布任务。返回任务 id。

        只接收作品 id,内部自开会话取记录,避免跨会话传入已绑定的 ORM 对象。
        target_platform: xhs / douyin / shipinhao。
        title/desc/topics 为 None 时沿用作品原始内容;传了则用编辑后的值(发布前可改)。
        """
        with get_session() as s:
            rec = s.get(ContentRecord, content_id)
            if not rec:
                return None
            files = self._content_files(rec)
            if not files:
                return None
            # 转发前若在弹窗里剔除/调序了图片,media_order 是保留下来的原始序号(按新顺序)。
            # 按它过滤+重排本地文件(首个=封面);越界序号忽略,全无效则回退全部原序。
            if media_order:
                picked = [files[i] for i in media_order
                          if isinstance(i, int) and 0 <= i < len(files)]
                if picked:
                    files = picked
            title_cap = {"douyin": 30, "shipinhao": 16}.get(target_platform, 20)
            t_title = (title if title is not None else (rec.desc or ""))[:title_cap]
            t_desc = desc if desc is not None else (rec.desc or "")
            t_topics = topics if topics is not None else ""
            task = PublishTask(
                platform=target_platform, account_id=account_id,
                media_type="video" if rec.media_type == "video" else "images",
                title=t_title, desc=t_desc, topics=t_topics,
                visibility=visibility, allow_save=allow_save,
                media_json=json.dumps(files),
                source_platform=rec.platform, source_content_id=rec.id,
            )
            s.add(task); s.commit(); s.refresh(task)
            return task.id

    async def _process_publish(self):
        due = []
        now = datetime.utcnow()
        with get_session() as s:
            tasks = s.exec(select(PublishTask)
                           .where(PublishTask.status == "pending")).all()
            for t in tasks:
                if t.scheduled_at is None or t.scheduled_at <= now:
                    due.append(t.id)
        for tid in due:
            await self.publish_task(tid)

    async def publish_task(self, task_id: int) -> dict:
        if task_id in self._publishing:
            return {"ok": False, "error": "正在发布中"}
        self._publishing.add(task_id)
        try:
            with get_session() as s:
                t = s.get(PublishTask, task_id)
                account_id = t.account_id if t else None
            # 发布串行 + 该账号串行(有头浏览器会接管该账号 profile,不能与抓取并发)
            async with self._publish_sem:
                async with self._operation_guard(
                        account_id, OperationKind.PUBLISH,
                        fallback_key=f"pub:{task_id}"):
                    return await self._publish_task_locked(task_id)
        finally:
            self._publishing.discard(task_id)

    async def _publish_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(PublishTask, task_id)
            if not t:
                return {"ok": False, "error": "任务不存在"}
            if t.status in ("done", "publishing"):
                return {"ok": False, "error": f"任务状态为 {t.status}"}
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            if not acc:
                t.status = "failed"
                t.error = "绑定账号不存在(可能已删除/重登成新号)"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if acc.status == "invalid":
                self._defer_row(t, "账号登录态已失效，等待重新登录", fallback_seconds=900)
                s.add(t); s.commit()
                return {"ok": False, "error": "account_invalid"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
            # 图文平台 / 抖音协议发布走纯 HTTP,压根不开浏览器,过不了
            # 「系统 Chrome + 有头页面 + 独立 Profile」这道为浏览器写操作设的门。
            # 抖音失败会回落浏览器,那时候再查环境。
            # 只豁免浏览器环境:代理、写暂停、活跃时段、风控 preflight 照走,账本统一。
            is_article = platform_spec(t.platform).kind == "article"
            skip_browser_env = is_article or t.platform == "douyin"
            environment_error = None if skip_browser_env else \
                self._native_write_environment_error(acc, headed=True, browser_mode=True)
            if environment_error:
                self._defer_row(t, environment_error, fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": environment_error}
            pause_error = self._write_pause_error(t.account_id)
            if pause_error:
                decision = self.risk.preflight(t.account_id, OperationKind.PUBLISH)
                self._defer_row(t, pause_error, decision.next_allowed_at)
                s.add(t); s.commit()
                return {"ok": False, "error": pause_error}
            if not self._in_active_window(t.account_id):
                self._defer_row(t, "当前处于非活跃时段，发布任务已保留在队列")
                s.add(t); s.commit()
                return {"ok": False, "error": t.error}
            decision = self.risk.preflight(t.account_id, OperationKind.PUBLISH)
            if not decision.allowed:
                self._defer_row(t, decision.reason, decision.next_allowed_at)
                s.add(t); s.commit()
                return {"ok": False, "error": decision.reason}
            # 发布用创作平台态;一次扫码已把创作 cookie 并入 storage_state,故回退它
            state = acc.creator_storage_state or acc.storage_state or ""
            native_mode = acc.identity_mode == "native"
            # 图文平台不开浏览器,identity 对它们没有意义(且 browser 可能未就绪)
            identity = None if is_article else self.browser.identity_for(acc)
            article_cookie = acc.cookie or ""
            article_proxy, article_ua = acc.proxy or "", acc.ua or ""
            t_account_id = t.account_id
            media_type, title, desc, topics = t.media_type, t.title, t.desc, t.topics
            visibility, allow_save = t.visibility, t.allow_save
            location = getattr(t, "location", "") or ""
            platform = t.platform
            files = _loads_list(t.media_json)
            t.status = "publishing"; t.error = ""
            s.add(t); s.commit()

        if platform in ARTICLE_KEYS:
            # 图文平台:纯协议发包,凭证是账号 Cookie(公众号是 "cookie||token" 两样)。
            # 正文走 PublishTask.desc(装 markdown),封面/正文图走 media_json。
            if not article_cookie:
                return await self._finish_publish(
                    task_id, False, "",
                    f"该账号没有 {platform_spec(platform).label} Cookie,"
                    f"请在账号页用「Cookie 登录」导入", platform=platform)
            fn = {"baijiahao": publish_baijiahao, "toutiao": publish_toutiao,
                  "wechat_mp": publish_wechat_mp}.get(platform)
            if fn is None:
                # registry 加了图文平台但这里没接分发:直接判失败,别让 KeyError
                # 逃出 try 块把任务永久卡在 publishing
                return await self._finish_publish(
                    task_id, False, "",
                    f"{platform} 已注册为图文平台但发布分发未接入(engine/monitor.py)",
                    platform=platform)
            try:
                ok, url, err = await fn(
                    article_cookie, title, desc, files,
                    topics=topics, proxy=article_proxy, ua=article_ua,
                    account_id=t_account_id, publish=True)
            except Exception as e:
                ok, url, err = False, "", f"发布异常: {e!r}"
            return await self._finish_publish(task_id, ok, url, err, platform=platform)

        if platform == "kuaishou":
            # 快手发布:登录态在该账号持久 profile 里(creator/storage 任一即可),走浏览器自动化
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成快手「创作者登录」,请先在账号页点「创作者登录」")
            try:
                ok, url, err = await publish_kuaishou(self.browser, identity, state,
                                                      media_type, title, desc, files,
                                                      topics=topics, headed=True)
            except Exception as e:
                ok, url, err = False, "", f"发布异常: {e!r}"
            return await self._finish_publish(task_id, ok, url, err, platform="kuaishou")

        if platform == "shipinhao":
            # 视频号发布:登录态在该账号持久 profile 里,走浏览器自动化(wujie shadowRoot)
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成视频号登录,请先在账号页点「视频号登录」")
            try:
                ok, url, err = await publish_channels(self.browser, identity, state,
                                                      media_type, title, desc, files,
                                                      topics=topics, headed=True,
                                                      location=location)
            except Exception as e:
                ok, url, err = False, "", f"发布异常: {e!r}"
            return await self._finish_publish(task_id, ok, url, err, platform="shipinhao")

        if platform == "douyin":
            # 视频走纯协议 create_v2(2026-08-17 抓包真源)。失败再回落有头浏览器。
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成抖音「创作者登录」,请先在账号页点「创作者登录」")
            video = next((f for f in files if f), "")
            if media_type != "images" and video:
                try:
                    ok, url, err = await publish_via_http(
                        state, video, title, desc, topics=topics,
                        visibility=visibility, allow_save=allow_save,
                        ua=article_ua, proxy=article_proxy)
                except Exception as e:
                    ok, url, err = False, "", f"协议发布异常: {e!r}"
                if ok:
                    return await self._finish_publish(
                        task_id, ok, url, err, platform="douyin")
                log.warning("抖音纯协议失败,回落浏览器: %s", err)
            with get_session() as s:
                acc2 = s.get(DouyinAccount, t_account_id)
                env_err = self._native_write_environment_error(
                    acc2, headed=True, browser_mode=True)
            if env_err:
                return await self._finish_publish(
                    task_id, False, "", env_err, platform="douyin")
            try:
                ok, url, err = await publish_douyin(
                    self.browser, identity, state,
                    media_type, title, desc, files,
                    topics=topics, visibility=visibility,
                    allow_save=allow_save, headed=True)
            except Exception as e:
                ok, url, err = False, "", f"发布异常: {e!r}"
            return await self._finish_publish(task_id, ok, url, err, platform="douyin")

        if not state:
            return await self._finish_publish(
                task_id, False, "", "该账号未完成小红书「创作者登录」,请先在账号页点「创作者登录」")

        xhs_mode = ("browser" if native_mode
                    else self._xhs_publish_mode())
        try:
            ok, url, err = await publish_xhs(self.browser, identity, state, media_type,
                                             title, desc, files, topics=topics,
                                             headed=True,
                                             mode=xhs_mode,
                                             on_submit=(
                                                 lambda: self._mark_browser_submit(
                                                     PublishTask, task_id)
                                                 if xhs_mode == "browser" else None))
        except Exception as e:
            ok, url, err = False, "", f"发布异常: {e!r}"
        return await self._finish_publish(task_id, ok, url, err)

    async def _finish_publish(self, task_id, ok, url, err, platform="xhs") -> dict:
        account_id = None
        failure = None
        uncertain = (not ok and isinstance(err, str)
                     and err.startswith("write_uncertain:"))
        if not ok and not uncertain:
            with get_session() as s:
                task = s.get(PublishTask, task_id)
                account_id = task.account_id if task else None
            if account_id:
                failure = self.risk.record_failure(
                    account_id, OperationKind.PUBLISH, err)
        with get_session() as s:
            t = s.get(PublishTask, task_id)
            if t:
                account_id = t.account_id
                if ok:
                    t.status = "done"
                    t.done_at = datetime.utcnow()
                elif uncertain:
                    # Submission crossed the click boundary but success evidence
                    # was lost.  Never enqueue it again automatically.
                    t.status = "uncertain"
                    t.scheduled_at = None
                    t.done_at = None
                elif failure and failure.controlled and failure.category in {
                        RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH}:
                    self._defer_row(t, err, failure.next_allowed_at)
                else:
                    t.status = "failed"
                t.result_url = url or t.result_url
                t.error = "" if ok else err
                s.add(t); s.commit()
        if ok and account_id:
            self.risk.record_success(account_id, OperationKind.PUBLISH)
        if ok:
            try:
                with get_session() as s:
                    chans = s.exec(select(NotificationChannel)
                                   .where(NotificationChannel.enabled == True)).all()  # noqa: E712
                    channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
                if channels:
                    pname = {"kuaishou": "快手", "douyin": "抖音",
                             "shipinhao": "视频号"}.get(platform, "小红书")
                    await notify_all(channels, f"{pname}发布成功", url or "已发布一条作品")
            except Exception as e:
                log.warning(f"发布成功但通知发送失败: {e!r}")
        return {"ok": ok, "url": url, "error": err}
