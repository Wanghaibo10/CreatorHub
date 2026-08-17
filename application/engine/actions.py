"""写操作队列:取关/回关/发私信的频控与执行。

MonitorEngine 的 Mixin(2026-08-17 从 monitor.py 拆出):方法仍属同一个引擎
实例,self 状态共享不变;拆分只为按职责分文件。
"""
from __future__ import annotations

from __future__ import annotations
from datetime import datetime
from sqlmodel import select
from application.browser import do_follow, send_dm, send_dm_api
from moss.common.db import get_session
from moss.model import DouyinAccount, AccountActionTask, FollowEdge, DmConversation
from moss.core.risk import classify_platform_error, OperationKind, RiskCategory

from application.engine._helpers import log


class ActionOps:
    def _action_gap_ok(self, account_id, gap: int) -> bool:
        """距该账号上一次成功写操作是否已超过最小间隔(防同账号连发)。
        实际间隔取「任务级 min_gap」与「全局 action_min_gap_seconds」的较大者。"""
        if not account_id:
            return True
        gap = max(gap, self.cfg.engine.action_min_gap_seconds)
        if gap <= 0:
            return True
        with get_session() as s:
            rows = s.exec(select(AccountActionTask.done_at)
                          .where(AccountActionTask.account_id == account_id)
                          .where(AccountActionTask.status == "done")).all()
        last = max([d for d in rows if d] or [None])
        return last is None or (datetime.utcnow() - last).total_seconds() >= gap

    def _action_count_since(self, account_id, since: datetime) -> int:
        """该账号自 since 起已成功执行的写操作数(用于每日/每小时上限)。"""
        if not account_id:
            return 0
        with get_session() as s:
            return len(s.exec(select(AccountActionTask.id)
                              .where(AccountActionTask.account_id == account_id)
                              .where(AccountActionTask.status == "done")
                              .where(AccountActionTask.done_at >= since)).all())

    def _action_cap_ok(self, account_id) -> bool:
        """写操作是否还在每日 / 每小时配额内(关注取关是封号重灾区,双重限流)。"""
        dcap = self.cfg.engine.action_daily_cap_per_account
        hcap = self.cfg.engine.action_hourly_cap_per_account
        if dcap > 0 and self._action_count_since(
                account_id, self._today_start(account_id)) >= dcap:
            return False
        if hcap > 0 and self._action_count_since(account_id, self._hour_ago()) >= hcap:
            return False
        return True

    def _action_gate_error(self, account_id, gap: int, action: str = "follow") -> str:
        """Apply the same write gate to queued and API-triggered actions."""
        pause_error = self._write_pause_error(account_id)
        if pause_error:
            return pause_error
        if not self._in_active_window(account_id):
            return "当前处于非活跃时段，写操作已保留在队列"
        if not self._action_cap_ok(account_id):
            return "已达到账号写操作额度"
        if not self._action_gap_ok(account_id, gap):
            return "尚未达到账号写操作最小间隔"
        kind = OperationKind.DM if action == "send_dm" else OperationKind.SOCIAL
        decision = self.risk.preflight(account_id, kind)
        if not decision.allowed:
            return decision.reason
        return ""

    async def _process_action_tasks(self):
        now = datetime.utcnow()
        due = []
        with get_session() as s:
            tasks = s.exec(select(AccountActionTask).where(
                AccountActionTask.status == "pending")).all()
            for t in tasks:
                if t.scheduled_at is None or t.scheduled_at <= now:
                    due.append((t.id, t.account_id, t.min_gap_seconds))
        seen_acct = set()
        for tid, aid, gap in due:
            # 同账号每轮最多执行一条,尊重最小间隔 + 每日/每小时配额(其余下轮再发)
            if aid in seen_acct or not self._action_gap_ok(aid, gap):
                continue
            if not self._action_cap_ok(aid):
                continue
            seen_acct.add(aid)
            try:
                await self.execute_action_task(tid)
            except Exception as e:
                log.warning("写操作任务 %s 执行异常: %s", tid, e)

    async def execute_action_task(self, task_id: int) -> dict:
        if task_id in self._actioning:
            return {"ok": False, "error": "正在执行中"}
        self._actioning.add(task_id)
        try:
            with get_session() as s:
                t = s.get(AccountActionTask, task_id)
                account_id = t.account_id if t else None
                kind = (OperationKind.DM if t and t.action == "send_dm"
                        else OperationKind.SOCIAL)
            async with self._operation_guard(
                    account_id, kind, fallback_key=f"act:{task_id}"):
                return await self._execute_action_task_locked(task_id)
        finally:
            self._actioning.discard(task_id)

    async def _execute_action_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(AccountActionTask, task_id)
            if not t or t.status != "pending":
                return {"ok": False, "error": "任务不可执行"}
            account_id = t.account_id
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            if not acc:
                t.status = "failed"; t.error = "绑定账号不存在(可能已删除/重登成新号)"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
            environment_error = self._native_write_environment_error(
                acc, headed=True, browser_mode=True)
            if environment_error:
                self._defer_row(t, environment_error, fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": environment_error}
            if acc.status == "invalid":
                self._defer_row(t, "账号登录态已失效，等待重新登录", fallback_seconds=900)
                s.add(t); s.commit()
                return {"ok": False, "error": "account_invalid"}
            gate_error = self._action_gate_error(
                t.account_id, t.min_gap_seconds, t.action)
            if gate_error:
                kind = (OperationKind.DM if t.action == "send_dm"
                        else OperationKind.SOCIAL)
                decision = self.risk.preflight(t.account_id, kind)
                self._defer_row(t, gate_error, decision.next_allowed_at)
                s.add(t); s.commit()
                return {"ok": False, "error": gate_error}
            action = t.action
            target_uid, target_sec_uid, content = t.target_uid, t.target_sec_uid, t.content
            platform = t.platform
            # 抖音发私信优先走无头 API(imapi/send):取会话的 short_id+ticket
            dm_conv_id, dm_short_id, dm_ticket = t.conv_id, "", ""
            if action == "send_dm" and platform == "douyin" and t.conv_id:
                _conv = s.exec(select(DmConversation).where(
                    DmConversation.account_id == t.account_id,
                    DmConversation.conv_id == t.conv_id)).first()
                if _conv:
                    dm_short_id, dm_ticket = _conv.conv_short_id, _conv.ticket
            # commit 会 expire 本 session 内的实例,先把所需原语取出来再 commit
            native_mode = acc.identity_mode == "native"
            identity = self.browser.identity_for(acc)
            t.status = "doing"; t.method = "browser"; t.error = ""
            s.add(t); s.commit()

        try:
            if action == "follow":
                ok, err = await do_follow(self.browser, identity, platform,
                                          target_uid, target_sec_uid)
            elif action == "unfollow":
                ok, err = await do_follow(self.browser, identity, platform,
                                          target_uid, target_sec_uid, unfollow=True)
            elif action == "send_dm":
                # 抖音:有会话信息就走无头 API 发送;失败或缺信息再回退 UI 自动化
                if (platform == "douyin" and not native_mode
                        and dm_conv_id and dm_short_id and dm_ticket):
                    ok, err = await send_dm_api(self.browser, identity, dm_conv_id,
                                                dm_short_id, dm_ticket, content)
                    category, _signal = classify_platform_error(err)
                    if not ok and category == RiskCategory.BUSINESS:
                        ok, err = await send_dm(self.browser, identity, platform,
                                                target_uid, target_sec_uid, content)
                else:
                    if platform == "xhs":
                        ok, err = await send_dm(
                            self.browser, identity, platform,
                            target_uid, target_sec_uid, content,
                            on_submit=lambda: self._mark_browser_submit(
                                AccountActionTask, task_id),
                        )
                    else:
                        ok, err = await send_dm(
                            self.browser, identity, platform,
                            target_uid, target_sec_uid, content)
            else:
                ok, err = False, f"未知动作 {action}"
        except Exception as e:
            ok, err = False, f"{e!r}"

        kind = OperationKind.DM if action == "send_dm" else OperationKind.SOCIAL
        uncertain = (
            not ok
            and platform == "xhs"
            and action == "send_dm"
            and str(err or "").startswith("write_uncertain:")
        )
        failure = None if ok or uncertain else self.risk.record_failure(
            account_id, kind, err)
        with get_session() as s:
            t = s.get(AccountActionTask, task_id)
            account_id = t.account_id if t else None
            if t:
                if ok:
                    t.status = "done"
                elif uncertain:
                    t.status = "uncertain"
                    t.scheduled_at = None
                    t.done_at = None
                elif failure and failure.controlled and failure.category in {
                        RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH}:
                    self._defer_row(t, err, failure.next_allowed_at)
                else:
                    t.status = "failed"
                t.error = "" if ok else err
                t.result = "ok" if ok else ""
                t.done_at = datetime.utcnow() if ok else t.done_at
                s.add(t); s.commit()
                if ok and action in ("follow", "unfollow"):
                    # 同一个人可能同时有两行:关注列表(following)+ 粉丝列表(fan)。
                    # 两行都要维护 —— 回关是在粉丝列表点的,只动 following 行的话
                    # 粉丝列表那行 is_following 还是 0,界面继续显示「未关注」。
                    def _edge(direction: str):
                        return s.exec(select(FollowEdge).where(
                            FollowEdge.account_id == t.account_id,
                            FollowEdge.platform == t.platform,
                            FollowEdge.direction == direction,
                            FollowEdge.uid == target_uid)).first()

                    edge, fan = _edge("following"), _edge("fan")
                    if action == "unfollow":
                        # 关注列表按 direction 取行,不看 is_following,
                        # 只翻标记的话取关成功后这人还挂在列表里。
                        if edge:
                            s.delete(edge)
                        if fan:      # ta 还关注我,但已不再互关
                            fan.is_following = False
                            fan.is_mutual = False
                            s.add(fan)
                    else:
                        if edge:
                            edge.is_following = True
                            s.add(edge)
                        else:        # 回关的人本来不在关注列表里,补一行
                            s.add(FollowEdge(
                                platform=t.platform, account_id=t.account_id,
                                direction="following", uid=target_uid,
                                sec_uid=target_sec_uid, nickname=t.target_nick,
                                avatar=fan.avatar if fan else "",
                                signature=fan.signature if fan else "",
                                is_following=True, is_mutual=bool(fan),
                                fetched_at=datetime.utcnow()))
                        if fan:      # 回关 ta = 互关
                            fan.is_following = True
                            fan.is_mutual = True
                            s.add(fan)
                    s.commit()
        if ok and account_id:
            self.risk.record_success(account_id, kind)
        return {"ok": ok, "error": "" if ok else err}
