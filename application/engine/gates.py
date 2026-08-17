"""闸门与风控辅助:账号锁/统一读闸门/代理与环境校验/写暂停/活跃时段。

MonitorEngine 的 Mixin(2026-08-17 从 monitor.py 拆出):方法仍属同一个引擎
实例,self 状态共享不变;拆分只为按职责分文件。
"""
from __future__ import annotations

from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from moss.common.db import get_session
from moss.model import DouyinAccount
from moss.core.risk import OperationKind

from application.engine._helpers import log


class GateOps:
    @staticmethod
    def _load_account(account_id):
        if not account_id:
            return None
        with get_session() as s:
            return s.get(DouyinAccount, account_id)

    @asynccontextmanager
    async def _operation_guard(self, account_id, kind: OperationKind,
                               fallback_key: str = "", operation_target=None):
        """Serialize by global limit, network exit, then account profile."""
        account = operation_target or self._load_account(account_id)
        key = f"acc:{account_id}" if account_id else (fallback_key or "anon")
        lock = self.browser.lock_for(key)
        async with self._active_sem:
            async with self.risk.network_guard(account):
                async with lock:
                    yield account

    @asynccontextmanager
    async def operation_guard(self, account_id, kind: OperationKind,
                              fallback_key: str = "", operation_target=None):
        """Public unified gate for platform operations owned by API routes."""
        async with self._operation_guard(
                account_id, kind, fallback_key, operation_target) as account:
            yield account

    @asynccontextmanager
    async def _account_guard(self, account_id, fallback_key: str = ""):
        """Compatibility wrapper for read call sites not converted yet."""
        async with self._operation_guard(
                account_id, OperationKind.READ_LIGHT, fallback_key) as account:
            yield account

    async def _guarded_read_dict(self, account_id, kind: OperationKind,
                                 fallback_key: str, operation) -> dict:
        """Run one read through its budget and persist the logical outcome."""
        decision = self.risk.preflight(account_id, kind)
        if not decision.allowed:
            return {
                "ok": True,
                "skipped": True,
                "reason": decision.reason,
                "next_allowed_at": (
                    decision.next_allowed_at.isoformat()
                    if decision.next_allowed_at else None),
            }
        try:
            async with self._operation_guard(
                    account_id, kind, fallback_key=fallback_key):
                decision = self.risk.preflight(account_id, kind)
                if not decision.allowed:
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": decision.reason,
                        "next_allowed_at": (
                            decision.next_allowed_at.isoformat()
                            if decision.next_allowed_at else None),
                    }
                result = await operation()
                error = result.get("error")
                if account_id:
                    if result.get("ok") and not result.get("skipped"):
                        self.risk.record_success(account_id, kind)
                    elif error:
                        self.risk.record_failure(
                            account_id, kind, error)
                if isinstance(error, BaseException):
                    result = dict(result)
                    result["error"] = str(error)
        except Exception as exc:
            if account_id:
                self.risk.record_failure(account_id, kind, exc)
            raise
        return result

    async def guarded_read_pair(self, account_id, kind: OperationKind,
                                fallback_key: str, operation, *, empty_result):
        """Budget a direct read returning ``(payload, error)``."""
        decision = self.risk.preflight(account_id, kind)
        if not decision.allowed:
            return empty_result, f"risk_deferred:{decision.reason}"
        try:
            async with self._operation_guard(
                    account_id, kind, fallback_key=fallback_key):
                decision = self.risk.preflight(account_id, kind)
                if not decision.allowed:
                    return empty_result, f"risk_deferred:{decision.reason}"
                payload, error = await operation()
                if account_id:
                    if not error or error == "empty":
                        self.risk.record_success(account_id, kind)
                    else:
                        self.risk.record_failure(account_id, kind, error)
                if isinstance(error, BaseException):
                    error = str(error)
        except Exception as exc:
            if account_id:
                self.risk.record_failure(account_id, kind, exc)
            return empty_result, repr(exc)
        return payload, error

    def _identity_proxy(self, acc):
        """由账号行构建 (Identity, proxy)。acc 为空则匿名画像。"""
        if acc:
            ident = self.browser.identity_for(acc)
            return ident, (acc.proxy or "")
        return self.browser.anon_identity(), ""

    def _dl_proxy(self, proxy: str) -> str:
        """媒体下载实际使用的代理(受 route_download_via_proxy 开关控制)。"""
        return proxy if self.cfg.engine.route_download_via_proxy else ""

    @staticmethod
    def _proxy_bad(acc) -> bool:
        return bool(
            acc and acc.proxy
            and acc.proxy_status in {"bad", "auth_error", "blocked", "drifted"}
        )

    def _native_write_environment_error(
            self, acc, *, headed: bool = True,
            browser_mode: bool = True) -> str:
        """Run the BrowserManager's native-only hard gate when supported."""
        checker = getattr(self.browser, "native_write_gate_error", None)
        if not callable(checker) or acc is None:
            return ""
        return str(checker(
            acc, headed=headed, browser_mode=browser_mode) or "")

    @staticmethod
    def _defer_row(row, reason: str, next_at: datetime | None = None,
                   fallback_seconds: int = 300) -> None:
        now = datetime.utcnow()
        proposed = next_at or (now + timedelta(seconds=max(1, fallback_seconds)))
        if row.scheduled_at is None or row.scheduled_at < proposed:
            row.scheduled_at = proposed
        row.status = "pending"
        row.error = str(reason or "平台操作已延后").strip()[:500]

    def _xhs_comment_write_mode(self) -> str:
        """Return the explicitly selected XHS comment write mode.

        Browser page writes are the default. Direct signed comment POSTs stay
        opt-in, while ``manual`` keeps the existing draft-only workflow.
        """
        mode = str(getattr(self.cfg.engine, "xhs_comment_write_mode", "browser")
                   or "browser").strip().lower()
        return mode if mode in {"browser", "api", "manual"} else "browser"

    def _xhs_publish_mode(self) -> str:
        """Use visible page publishing unless API compatibility is explicit."""
        mode = str(getattr(self.cfg.engine, "xhs_publish_mode", "browser")
                   or "browser").strip().lower()
        return mode if mode in {"browser", "api"} else "browser"

    def _write_pause_error(self, account_id) -> str:
        """Return a persisted account write pause, clearing an expired one."""
        if not self.risk.policy.enabled:
            return ""
        if not account_id:
            return ""
        now = datetime.utcnow()
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if not acc:
                return ""
            until = acc.write_paused_until
            if until and until > now:
                reason = (acc.write_pause_reason or "平台拒绝写操作").strip()
                return f"账号写操作已暂停至 {until.isoformat(timespec='seconds')}: {reason[:120]}"
            if until:
                acc.write_paused_until = None
                acc.write_pause_reason = ""
                s.add(acc)
                s.commit()
        return ""

    def _prune_risk_events_if_due(self, now: datetime | None = None) -> int:
        """Prune retained risk events once for each attempted UTC day."""
        now = now or datetime.utcnow()
        prune_day = now.date()
        if self._last_risk_prune_day is not None \
                and prune_day <= self._last_risk_prune_day:
            return 0
        self._last_risk_prune_day = prune_day
        try:
            return self.risk.prune_events(now=now)
        except Exception:
            log.exception("risk event pruning failed for %s", prune_day.isoformat())
            return 0

    def _in_active_window(self, account_id=None) -> bool:
        """Check active hours in the bound account's persisted timezone."""
        if not self.cfg.engine.quiet_hours_enabled:
            return True
        account = self._load_account(account_id)
        if account is not None:
            return self.risk._in_active_window(account, datetime.utcnow())
        start = self.cfg.engine.active_hours_start
        end = self.cfg.engine.active_hours_end
        if end <= start:
            return True
        h = (datetime.utcnow() + timedelta(hours=8)).hour   # 东八区(账号默认时区)
        if end <= 24:
            return start <= h < end
        return h >= start or h < (end - 24)                 # 跨零点

    def _today_start(self, account_id=None) -> datetime:
        n = datetime.utcnow()
        account = self._load_account(account_id)
        if account is not None:
            return self.risk._local_day_start_utc(account, n)
        return datetime(n.year, n.month, n.day)

    @staticmethod
    def _hour_ago() -> datetime:
        return datetime.utcnow() - timedelta(hours=1)
