"""账号管理 API(列表/删除/资料刷新/代理绑定/浏览器窗口)。

从 main.py 抽出(2026-08-17 模块化)。资料抓取在 services/account_profile.py,
窗口租约在 services/browser_windows.py,代理探测在 services/proxy.py。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from moss.core.config import get_config
from moss.common.db import get_session
from moss.common.logging_setup import get_logger
from moss.model import (AccountRiskState, AccountWork, DmConversation, DouyinAccount, FollowEdge, MonitorTarget, RiskEvent)
from application.registry import SPECS as PLATFORM_SPECS, spec as PLATFORM_SPEC
from application.xhs import has_creator_cookies
from app.service.profiles import assign_proxy_from_pool
from moss.core.risk import OperationKind, RiskController
from moss.core.runtime import rt
from app.service.account_ops import _run_account_read
from app.service import account_profile as _profile
from app.service.browser_windows import _OpenBrowserLease, _release_open_browser
from app.service.proxy import _mask_proxy, _probe_proxy, _proxy_status_from_detail

router = APIRouter(tags=["accounts"])
cfg = get_config()
log = get_logger("routers.accounts")
_PLATFORM_HOST = {k: s.host for k, s in PLATFORM_SPECS.items()}


class ProxyIn(BaseModel):
    proxy: str = ""


@router.get("/api/accounts")
async def list_accounts(platform: str | None = None):
    risk_controller = rt.engine.risk if rt.engine else RiskController(cfg)
    with get_session() as s:
        q = select(DouyinAccount)
        if platform:
            q = q.where(DouyinAccount.platform == platform)
        accs = s.exec(q).all()
        out = []
        for a in accs:
            risk_state = s.get(AccountRiskState, a.id) if a.id else None
            next_write_at = risk_controller.next_write_at(a.id) if a.id else None
            used = len(s.exec(select(MonitorTarget.id)
                              .where(MonitorTarget.account_id == a.id)).all())
            environment = None
            if a.platform == "xhs" and rt.browser is not None:
                try:
                    environment = rt.browser.environment_snapshot(
                        rt.browser.identity_for(a), headless=False)
                except Exception:
                    environment = None
            out.append({
                "id": a.id, "platform": a.platform, "nickname": a.nickname, "status": a.status,
                "sec_uid": a.sec_uid, "douyin_id": a.douyin_id, "avatar": a.avatar,
                "follower_count": a.follower_count, "aweme_count": a.aweme_count,
                "has_creator": bool(a.creator_storage_state) or has_creator_cookies(a.storage_state),
                "kind": "creator" if (a.creator_storage_state or has_creator_cookies(a.storage_state)) else "fetch",
                "has_storage": bool(a.storage_state),
                "login_type": "cookie" if a.cookie else "scan",
                "monitor_count": used,
                # 风控隔离画像
                "proxy": _mask_proxy(a.proxy),
                "proxy_status": a.proxy_status,
                "has_proxy": bool(a.proxy),
                "exit_ip": a.exit_ip,
                "exit_country": a.exit_country,
                "exit_asn": a.exit_asn,
                "exit_timezone": a.exit_timezone,
                "exit_checked_at": (a.exit_checked_at.isoformat()
                                    if a.exit_checked_at else None),
                "write_paused_until": (a.write_paused_until.isoformat()
                                        if a.write_paused_until else None),
                "write_pause_reason": a.write_pause_reason,
                "identity_mode": a.identity_mode,
                "risk_level": risk_state.risk_level if risk_state else 0,
                "risk_cooldown_until": (
                    risk_state.cooldown_until.isoformat()
                    if risk_state and risk_state.cooldown_until else None),
                "risk_signal": risk_state.last_risk_reason if risk_state else "",
                "next_write_at": (next_write_at.isoformat()
                                  if next_write_at else None),
                "ua": a.ua,
                "profile_dir": a.profile_dir,
                "environment": environment,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
        return out


@router.get("/api/accounts/{account_id}/environment")
async def account_browser_environment(account_id: int):
    """Return redacted browser-backend diagnostics for one account."""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account is None:
            raise HTTPException(404, "账号不存在")
        identity = rt.browser.identity_for(account)
    return rt.browser.environment_snapshot(identity, headless=False)


@router.delete("/api/accounts/{account_id}")
async def del_account(account_id: int):
    import shutil
    pdir = ""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if acc:
            pdir = acc.profile_dir or ""
            risk_state = s.get(AccountRiskState, account_id)
            if risk_state:
                s.delete(risk_state)
            for event in s.exec(select(RiskEvent).where(
                    RiskEvent.account_id == account_id)).all():
                s.delete(event)
            s.delete(acc)
            s.commit()
    # 删号同时清理其持久 profile(释放磁盘);代理回到池里(占用计数自然下降)
    if pdir:
        try:
            await rt.browser.close_context(account_id)
        except Exception:
            pass
        try:
            shutil.rmtree(pdir, ignore_errors=True)
        except Exception:
            pass
    return {"ok": True}


@router.post("/api/accounts/{account_id}/refresh-profile")
async def refresh_account_profile(account_id: int):
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        state = acc.storage_state or acc.creator_storage_state
        platform = acc.platform
    if not state:
        raise HTTPException(400, "该账号无浏览器登录态(Cookie 粘贴账号可能不含完整态),无法拉取资料")

    async def _refresh_profile():
        return await _profile._enrich_account_profile(
            account_id, state, detailed=True)

    res, outcome = await _run_account_read(
        account_id, OperationKind.READ_LIGHT,
        f"refresh-profile:{account_id}", _refresh_profile,
        empty_result={"ok": True})
    if isinstance(outcome, dict):
        return outcome
    if res == "invalid":
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    if res != "ok":
        tag = ("[xhs_self_profile]" if platform == "xhs"
               else "[ks_self_profile]" if platform == "kuaishou"
               else "[self_profile]")
        raise HTTPException(400, f"未能获取账号资料:请看服务端控制台 {tag} 那行日志"
                                 "(含它实际看到的请求),把它发我即可定位")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        return {"ok": True, "nickname": acc.nickname, "platform": acc.platform,
                "douyin_id": acc.douyin_id, "sec_uid": acc.sec_uid}


@router.get("/api/hub/summary")
async def hub_summary(account_id: int):
    with get_session() as s:
        def _n(q):
            return len(s.exec(q).all())
        return {
            "works": _n(select(AccountWork.id)
                        .where(AccountWork.account_id == account_id)),
            "following": _n(select(FollowEdge.id)
                            .where(FollowEdge.account_id == account_id,
                                   FollowEdge.direction == "following")),
            "fans": _n(select(FollowEdge.id)
                       .where(FollowEdge.account_id == account_id,
                              FollowEdge.direction == "fan")),
            "dm": _n(select(DmConversation.id)
                     .where(DmConversation.account_id == account_id)),
        }


def _platform_url_allowed(platform: str, value: str) -> bool:
    expected = _PLATFORM_HOST.get(platform, "").casefold()
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").casefold()
    except (TypeError, ValueError):
        return False
    return bool(
        expected and parsed.scheme in {"http", "https"}
        and (host == expected or host.endswith("." + expected))
    )


@router.post("/api/accounts/{account_id}/open-browser")
async def open_account_browser(account_id: int, url: str = ""):
    """用该账号登录态弹出一个真实浏览器窗口。默认停在平台首页;传 url 则停在该地址
    (仅允许本平台域名,用于「查看」视频号作品/管理页等需登录态才能打开的页面)。
    留给用户手动操作(查看/收发私信、F12 抓接口、手动维护等)。关闭窗口即落盘 Cookie。
    小红书复用账号专属 CDP Chrome，窗口开启期间占用全局可见操作队列；其他平台仍使用
    临时有头 Context。用完关闭窗口后，后台任务会继续执行。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        identity = rt.browser.identity_for(acc)
        states = [acc.storage_state or "", acc.creator_storage_state or ""]
    # 该账号已开着窗口就先关旧的(同一 profile 不能并存)
    old = rt.open_browsers.pop(account_id, None)
    if old is not None:
        try:
            await old.close()
        except Exception:
            pass
    home = PLATFORM_SPEC(platform).home_url
    # 传了 url 且属于本平台域名 -> 停在该地址(否则回首页,防被当跳转开任意站)
    tgt = (url or "").strip()
    if _platform_url_allowed(platform, tgt):
        home = tgt
    # 持久 profile 只在"首次空目录"才注入登录态;为防 profile 里 Cookie 缺失/过期导致
    # 打开后未登录,这里用 DB 里已知的登录态 Cookie 再注入一次(覆盖刷新)。
    from application.browser.manager import _sanitize_cookies
    cookies = []
    for st in states:
        if st:
            try:
                cookies.extend(json.loads(st).get("cookies") or [])
            except Exception:
                pass

    @asynccontextmanager
    async def _open_guard():
        @asynccontextmanager
        async def _engine_guard():
            if rt.engine is None:
                yield None
                return
            async with rt.engine.operation_guard(
                    account_id, OperationKind.LOGIN,
                    fallback_key=f"open-browser:{account_id}",
                    operation_target=identity) as guarded:
                yield guarded

        async with _engine_guard() as guarded:
            if platform == "xhs":
                # A user-held XHS window participates in the same machine-wide
                # visible-action queue as scheduled reads and writes.
                async with rt.browser.visible_action(identity):
                    yield guarded
            else:
                yield guarded

    guard = _open_guard()
    await guard.__aenter__()
    logged_out = False
    try:
        ctx = await rt.browser.open_headed(identity)
        if cookies:
            try:
                await ctx.add_cookies(_sanitize_cookies(cookies))
            except Exception as e:
                log.warning(f"注入 Cookie 失败: {e!r}")
        page = (await rt.browser.new_page(identity, block_media=False)
                if platform == "xhs" else await ctx.new_page())
        await page.goto(home, wait_until="domcontentloaded", timeout=30000)
        # Cookie 存在/未过期不代表服务端仍接受这次会话。若真实页面已回到登录
        # 地址或明确显示“登录”，立即同步账号状态，避免账号列表误报“正常”。
        current_url = str(getattr(page, "url", "") or "").lower()
        logged_out = (
            "passport" in current_url
            or "/login" in current_url
            or "login.html" in current_url
        )
        if not logged_out:
            try:
                await page.wait_for_timeout(1000)
                logged_out = await page.get_by_text(
                    "登录", exact=True).first.is_visible(timeout=1500)
            except Exception:
                logged_out = False
        if logged_out:
            with get_session() as s:
                opened_account = s.get(DouyinAccount, account_id)
                if opened_account:
                    opened_account.status = "invalid"
                    s.add(opened_account)
                    s.commit()
        if platform == "xhs":
            try:
                await page.bring_to_front()
            except Exception:
                pass
    except BaseException as e:
        await guard.__aexit__(type(e), e, e.__traceback__)
        if not isinstance(e, Exception):
            raise
        raise HTTPException(500, f"打开浏览器失败: {e!r}")
    close_callback = (
        (lambda: rt.browser.close_context(identity.key))
        if platform == "xhs" else None
    )
    lease = _OpenBrowserLease(ctx, guard, close_callback=close_callback)
    rt.open_browsers[account_id] = lease
    try:                       # 用户手动关窗后,从登记表移除
        ctx.on("close", lambda *_: asyncio.create_task(
            _release_open_browser(account_id, lease)))
        if platform == "xhs":
            page.on("close", lambda *_: asyncio.create_task(
                _release_open_browser(account_id, lease)))
    except Exception:
        pass
    return {"ok": True, "logged_out": logged_out}


def _reset_account_exit_baseline(acc) -> None:
    """A proxy assignment starts a new browser-egress baseline generation."""
    acc.exit_ip = ""
    acc.exit_country = ""
    acc.exit_asn = ""
    acc.exit_timezone = ""
    acc.exit_proxy_signature = ""
    acc.exit_checked_at = None


@router.put("/api/accounts/{account_id}/proxy")
async def set_account_proxy(account_id: int, body: ProxyIn):
    """手动设置/清空账号专属代理。改后会关掉该账号常驻 context,下次用新代理重开。"""
    from application.browser.manager import _parse_proxy, normalize_proxy
    p = (body.proxy or "").strip()
    if p and not _parse_proxy(p):
        raise HTTPException(400, "代理格式无法解析,示例:http://user:pass@host:port 或 socks5://host:port")
    p = normalize_proxy(p)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        acc.proxy = p
        acc.proxy_status = "unknown"
        _reset_account_exit_baseline(acc)
        s.add(acc); s.commit()
    if rt.browser:
        await rt.browser.close_context(account_id)
    return {"ok": True, "proxy": _mask_proxy(p)}


@router.post("/api/accounts/{account_id}/clear-write-pause")
async def clear_account_write_pause(account_id: int):
    """Clear the persisted write pause after the account has been checked manually."""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
    (rt.engine.risk if rt.engine else RiskController(cfg)).clear_account(account_id)
    return {"ok": True}


@router.post("/api/accounts/{account_id}/assign-proxy")
async def assign_account_proxy(account_id: int):
    """从代理池(config.proxies)给该账号分配一条占用最少的代理。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        p = assign_proxy_from_pool(s, cfg)
        if not p:
            raise HTTPException(400, "代理池为空(请在 config.yaml 的 proxies 里配置)")
        acc.proxy = p
        acc.proxy_status = "unknown"
        _reset_account_exit_baseline(acc)
        s.add(acc); s.commit()
    if rt.browser:
        await rt.browser.close_context(account_id)
    return {"ok": True, "proxy": _mask_proxy(p)}


@router.post("/api/accounts/assign-proxies-all")
async def assign_proxies_all():
    """给所有「尚未配置代理」的账号从池里批量分配(占用最少优先,均衡)。
    池里代理不够时,分到没有为止,返回还差多少。"""
    assigned, pool_empty = 0, False
    with get_session() as s:
        accs = [a.id for a in s.exec(select(DouyinAccount)).all() if not a.proxy]
    remaining = []
    for aid in accs:
        with get_session() as s:
            acc = s.get(DouyinAccount, aid)
            if not acc or acc.proxy:
                continue
            p = assign_proxy_from_pool(s, cfg)   # 每次重算占用,保持均衡
            if not p:
                pool_empty = True
                remaining.append(aid)
                continue
            acc.proxy = p
            acc.proxy_status = "unknown"
            _reset_account_exit_baseline(acc)
            s.add(acc); s.commit()
            assigned += 1
        if rt.browser:
            await rt.browser.close_context(aid)
    if pool_empty and assigned == 0:
        raise HTTPException(400, "代理池为空,请先在「代理池」添加代理")
    return {"ok": True, "assigned": assigned, "unassigned": len(remaining)}


@router.post("/api/accounts/{account_id}/test-proxy")
async def test_account_proxy(account_id: int):
    """用 native 账号的真实 BrowserContext 验证代理出口并建立基线。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        proxy = acc.proxy or ""
        platform = acc.platform
        identity_mode = acc.identity_mode
        identity = rt.browser.identity_for(acc) if rt.browser else None
    if not proxy:
        return {"ok": False, "detail": "该账号未配置代理(将走宿主真实 IP)"}
    browser_exit = None
    if identity_mode == "native" and rt.browser is not None and identity is not None:
        try:
            # 强制下次 context 使用数据库中最新的代理配置。
            await rt.browser.close_context(account_id)
            browser_exit = await rt.browser.probe_browser_exit(identity)
            ok = True
            detail = f"Browser IP {browser_exit['ip']}"
        except Exception as exc:
            ok = False
            detail = f"Browser probe failed: {exc}"
    else:
        ok, detail = await _probe_proxy(proxy, platform)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if acc:
            if ok and browser_exit:
                signature = rt.browser.proxy_signature(proxy)
                same_generation = bool(acc.exit_proxy_signature) \
                    and acc.exit_proxy_signature == signature
                drift = same_generation and any((
                    bool(acc.exit_ip and acc.exit_ip != browser_exit["ip"]),
                    bool(acc.exit_country and browser_exit["country"]
                         and acc.exit_country != browser_exit["country"]),
                    bool(acc.exit_asn and browser_exit["asn"]
                         and acc.exit_asn != browser_exit["asn"]),
                ))
                if drift:
                    acc.proxy_status = "drifted"
                    detail = (
                        f"浏览器出口漂移: 基线 {acc.exit_ip or '-'} / "
                        f"{acc.exit_asn or '-'}, 当前 {browser_exit['ip']} / "
                        f"{browser_exit['asn'] or '-'}")
                    ok = False
                else:
                    acc.proxy_status = "ok"
                    acc.exit_ip = browser_exit["ip"]
                    acc.exit_country = browser_exit["country"]
                    acc.exit_asn = browser_exit["asn"]
                    acc.exit_timezone = browser_exit["timezone"]
                    acc.exit_proxy_signature = signature
                    acc.exit_checked_at = datetime.utcnow()
            else:
                acc.proxy_status = _proxy_status_from_detail(ok, detail)
            s.add(acc); s.commit()
    return {"ok": ok, "detail": detail, "proxy": _mask_proxy(proxy),
            "browser_exit": browser_exit}
