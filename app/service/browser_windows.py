"""手动打开的账号浏览器窗口:租约管理与原生 UA 回写。

从 main.py 抽出(2026-08-17 模块化):窗口引用存 rt.open_browsers,
_persist_native_ua 由 lifespan 传给 BrowserManager 做回调。
"""
from __future__ import annotations

import asyncio

from moss.common.db import get_session
from moss.model import DouyinAccount
from moss.core.runtime import rt


class _OpenBrowserLease:
    """Keep the unified account/network guard until a headed context closes."""

    def __init__(self, context, guard, close_callback=None):
        self.context = context
        self.guard = guard
        self.close_callback = close_callback
        self._released = False
        self._closed = False
        self._release_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            self._released = True
            await self.guard.__aexit__(None, None, None)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self.close_callback is not None:
                    await self.close_callback()
                else:
                    await self.context.close()
            finally:
                await self.release()


async def _release_open_browser(account_id: int, lease: _OpenBrowserLease) -> None:
    try:
        await lease.close()
    finally:
        if rt.open_browsers.get(account_id) is lease:
            rt.open_browsers.pop(account_id, None)


def _persist_native_ua(account_id: int, ua: str) -> None:
    """Persist the UA observed from an account's native Chromium context."""
    value = str(ua or "").strip()
    if not account_id or not value:
        return
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account and account.identity_mode == "native" and account.ua != value:
            account.ua = value
            session.add(account)
            session.commit()
