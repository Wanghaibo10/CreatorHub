"""扫码/Cookie 登录 API(真实浏览器)。

从 main.py 抽出(2026-08-17 模块化)。登录任务状态在 rt.login_tasks,
资料抓取在 services/account_profile.py。
"""
from __future__ import annotations

import asyncio
import traceback
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from application.browser import (cookie_string_to_state, interactive_channels_login, interactive_creator_login, interactive_ks_creator_login, interactive_ks_login, interactive_login, interactive_xhs_creator_login, interactive_xhs_login)
from moss.core.config import get_config
from moss.common.db import get_session
from moss.model import DouyinAccount
from application.registry import (ARTICLE_KEYS as ARTICLE_PLATFORM_KEYS, COOKIE_LOGIN_KEYS, normalize as normalize_platform, spec as PLATFORM_SPEC)
from app.service.profiles import (ensure_identity, release_proxy_reservation, reserve_proxy_from_pool)
from moss.core.risk import OperationKind
from moss.core.runtime import rt
from app.service import account_profile as _profile

router = APIRouter(tags=["login"])
cfg = get_config()


async def _run_login(task_id: str, creator: bool = False, account_id: int | None = None,
                     platform: str = "douyin", proxy_choice: str = "auto"):
    """扫码登录。多账号隔离模型:一账号=一持久 profile。
    - 传 account_id:登录进该账号自己的 profile(重新登录/补创作者登录)。
    - 不传:用「临时 profile」登录,**只有登录成功才建账号**;关窗/超时/取消都不留残号。"""
    import os
    import shutil
    from application.browser import Identity, generate_identity_fields
    rt.login_tasks[task_id] = {"status": "waiting"}
    fresh_account = account_id is None
    tmp_profile = ""
    proxy_reservation_key = ""
    new_fields = None
    login_environment = {}
    nm = ("小红书账号" if platform == "xhs"
          else "快手账号" if platform == "kuaishou"
          else "视频号账号" if platform == "shipinhao"
          else "创作者账号" if creator else "扫码账号")
    try:
        # 1) 准备画像 + identity(新建账号此时不写库,只用临时 profile)
        if account_id:
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if not acc:
                    rt.login_tasks[task_id] = {"status": "error", "error": "账号不存在"}
                    return
                ensure_identity(acc, cfg, session=s, assign_proxy=False)
                s.add(acc); s.commit(); s.refresh(acc)
                identity = rt.browser.identity_for(acc)
                acc_id = acc.id
        else:
            acc_id = None
            new_fields = generate_identity_fields()
            tmp_profile = os.path.join(cfg.engine.profiles_dir, "new_" + uuid.uuid4().hex)
            # 登录前选定代理:具体地址 / auto(占用最少) / 空(不用代理)
            choice = (proxy_choice or "").strip()
            if choice.lower() in ("", "none"):
                proxy = ""
            elif choice.lower() == "auto":
                with get_session() as s:
                    proxy_reservation_key = task_id
                    proxy = reserve_proxy_from_pool(s, cfg, proxy_reservation_key)
            else:
                from application.browser.manager import normalize_proxy
                proxy = normalize_proxy(choice)
            identity = Identity(
                account_id=None, profile_dir=tmp_profile, identity_mode="native",
                platform=platform,
                proxy=proxy, ua="", viewport_w=new_fields["viewport_w"],
                viewport_h=new_fields["viewport_h"], timezone_id=new_fields["timezone_id"],
                locale=new_fields["locale"], fp_seed=new_fields["fp_seed"])

        # 登录轮询返回可诊断但不含代理凭据的实际运行环境。浏览器或版本回退
        # 一眼可见，避免把平台验证误判成单纯的 Cookie/二维码问题。
        login_environment = rt.browser.environment_snapshot(identity, headless=False)
        rt.login_tasks[task_id] = {
            "status": "waiting",
            "environment": login_environment,
        }

        # 2) 在统一账号/网络入口内扫码，避免与后台任务并行占用 profile。
        @asynccontextmanager
        async def _login_guard():
            if rt.engine is None:
                yield None
                return
            async with rt.engine.operation_guard(
                    account_id, OperationKind.LOGIN,
                    fallback_key=f"login:{task_id}",
                    operation_target=identity) as guarded:
                yield guarded

        async with _login_guard():
            if platform == "xhs":
                reauth_options = {"force_reauth": True} if account_id else {}
                if creator:
                    ok, state_json, nickname = await interactive_xhs_creator_login(
                        rt.browser, identity, **reauth_options)
                else:
                    ok, state_json, nickname = await interactive_xhs_login(
                        rt.browser, identity, **reauth_options)
            elif platform == "kuaishou":
                reauth_options = {"force_reauth": True} if account_id else {}
                if creator:
                    ok, state_json, nickname = await interactive_ks_creator_login(
                        rt.browser, identity, **reauth_options)
                else:
                    ok, state_json, nickname = await interactive_ks_login(
                        rt.browser, identity, **reauth_options)
            elif platform == "shipinhao":
                reauth_options = {"force_reauth": True} if account_id else {}
                ok, state_json, nickname = await interactive_channels_login(
                    rt.browser, identity, **reauth_options)
            elif creator:
                reauth_options = {"force_reauth": True} if account_id else {}
                ok, state_json, nickname = await interactive_creator_login(
                    rt.browser, identity, **reauth_options)
            else:
                reauth_options = {"force_reauth": True} if account_id else {}
                ok, state_json, nickname = await interactive_login(
                    rt.browser, identity, **reauth_options)

        # 浏览器已经完成实际后端选择；此时快照能反映系统 Chrome 或真实回退。
        login_environment = rt.browser.environment_snapshot(identity, headless=False)
        if fresh_account and platform == "xhs":
            # The temporary identity has no durable account key. Close its CDP
            # session before persisting the account so the same profile can be
            # reopened under the newly assigned account id without conflict.
            await rt.browser.close_context(identity.key)

        # 3) 仅在成功时落库
        if ok and state_json:
            is_xhs = platform == "xhs"
            with get_session() as s:
                if account_id:
                    acc = s.get(DouyinAccount, acc_id)
                else:
                    acc = DouyinAccount(
                        platform=platform, nickname=nickname or nm, status="active",
                        profile_dir=tmp_profile, proxy=identity.proxy,
                        identity_mode="native", ua=identity.ua or "",
                        viewport_w=new_fields["viewport_w"],
                        viewport_h=new_fields["viewport_h"],
                        timezone_id=new_fields["timezone_id"], locale=new_fields["locale"],
                        fp_seed=new_fields["fp_seed"])
                    s.add(acc); s.commit(); s.refresh(acc); acc_id = acc.id
                if creator:
                    acc.creator_storage_state = state_json
                    if not is_xhs or not acc.storage_state:   # xhs 创作登录不覆盖读取态
                        acc.storage_state = state_json
                elif platform == "shipinhao":
                    # 视频号一套登录态即读取又发布,两处都写
                    acc.storage_state = state_json
                    acc.creator_storage_state = state_json
                else:
                    acc.storage_state = state_json
                if nickname:
                    acc.nickname = nickname
                if acc.identity_mode == "native" and identity.ua:
                    acc.ua = identity.ua
                acc.status = "active"
                s.add(acc); s.commit()

            # 登录态已经持久化且账号已经落库：立即通知前端把账号显示出来。
            # 完整资料抓取可能还需数秒，不能让它阻塞“扫码成功”的交互反馈。
            rt.login_tasks[task_id] = {
                "status": "persisted",
                "account_id": acc_id,
                "nickname": nickname or nm,
                "hint": "扫码已确认，正在校验登录态并同步账号资料",
                "environment": login_environment,
            }

            profile_status = await _profile._enrich_account_profile(acc_id, state_json)   # best-effort
            with get_session() as s:
                acc = s.get(DouyinAccount, acc_id)
                rt.login_tasks[task_id] = {"status": "confirmed", "account_id": acc_id,
                                        "nickname": acc.nickname if acc else (nickname or nm),
                                        "profile_status": profile_status,
                                        "environment": login_environment}
        else:
            if fresh_account and tmp_profile:   # 没建账号,清理临时 profile
                try:
                    await rt.browser.close_context(identity.key)
                except Exception:
                    pass
                shutil.rmtree(tmp_profile, ignore_errors=True)
            rt.login_tasks[task_id] = {
                "status": "expired",
                "environment": login_environment,
            }
    except Exception as e:
        traceback.print_exc()
        if fresh_account and tmp_profile:
            try:
                await rt.browser.close_context(identity.key)
            except Exception:
                pass
            shutil.rmtree(tmp_profile, ignore_errors=True)
        rt.login_tasks[task_id] = {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "environment": login_environment,
        }
    finally:
        if proxy_reservation_key:
            release_proxy_reservation(proxy_reservation_key)


@router.post("/api/login/browser/start")
async def login_browser_start(proxy: str = "auto"):
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开浏览器窗口,请在其中点击“登录”并用抖音 App 扫码"}


@router.post("/api/login/creator/start")
async def login_creator_start(proxy: str = "auto"):
    """创作中心登录(用于自有账号评论模式;其登录态同样可用于公开抓取)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=True, proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开创作中心窗口,请在其中扫码登录你的抖音号"}


@router.post("/api/login/xhs/start")
async def login_xhs_start(proxy: str = "auto"):
    """小红书扫码登录(用于监控/读取)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, platform="xhs", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开小红书官网首页,请在窗口中点击登录并用小红书 App 扫码"}


@router.post("/api/login/xhs-creator/start")
async def login_xhs_creator_start(proxy: str = "auto"):
    """小红书「创作服务平台」登录(用于发布/已发布列表)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=True, platform="xhs", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开小红书创作平台窗口,请扫码登录(发布用)"}


@router.post("/api/login/kuaishou/start")
async def login_ks_start(proxy: str = "auto"):
    """快手扫码登录(用于监控/读取)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, platform="kuaishou", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开快手窗口,请在其中用快手 App 扫码登录"}


@router.post("/api/login/kuaishou-creator/start")
async def login_ks_creator_start(proxy: str = "auto"):
    """快手「创作者服务平台」登录(用于发布)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=True, platform="kuaishou",
                                   proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开快手创作平台窗口,请扫码登录(发布用)"}


@router.post("/api/login/shipinhao/start")
async def login_channels_start(proxy: str = "auto"):
    """视频号扫码登录(读取/发布共用,微信扫码)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, platform="shipinhao", proxy_choice=proxy))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开视频号助手窗口,请用微信扫码登录"}


@router.get("/api/login/browser/poll")
async def login_browser_poll(task_id: str):
    info = rt.login_tasks.get(task_id)
    if not info:
        raise HTTPException(404, "task 不存在")
    if info.get("status") in ("confirmed", "error", "expired"):
        # 终态:取走后清理
        rt.login_tasks.pop(task_id, None)
    return info


class CookieIn(BaseModel):
    cookie: str
    nickname: str = ""
    platform: str = "douyin"            # 见 platforms/registry.py 的 COOKIE_LOGIN_KEYS
    token: str = ""                     # 公众号专用:token 不在 cookie 里,要单独给


@router.post("/api/login/cookie")
async def login_cookie(body: CookieIn):
    """Cookie 粘贴登录。

    对图文平台(百家号/头条/公众号)这是**唯一**登录方式——它们走纯 HTTP,
    没有扫码流程。公众号要 cookie + token 两样,合并成 "<cookie>||<token>" 存一列。
    """
    platform = normalize_platform(body.platform, allowed=COOKIE_LOGIN_KEYS)
    state = cookie_string_to_state(body.cookie, platform)
    raw_cookie = body.cookie.strip()
    if platform == "wechat_mp":
        if not body.token.strip():
            raise HTTPException(400, "公众号还需要 token(在后台页面 URL 的 token= 参数里)")
        raw_cookie = f"{raw_cookie}||{body.token.strip()}"
    with get_session() as s:
        acc = DouyinAccount(
            nickname=body.nickname or f"{PLATFORM_SPEC(platform).label}账号",
            platform=platform,
            identity_mode="native", cookie=raw_cookie, storage_state=state)
        s.add(acc); s.commit(); s.refresh(acc)
        # 分配画像(profile/UA/指纹/代理):Cookie 会在首次开持久 profile 时桥接注入
        ensure_identity(acc, cfg, session=s, assign_proxy=True)
        s.add(acc); s.commit(); s.refresh(acc)
        return {"account_id": acc.id, "nickname": acc.nickname}


@router.post("/api/accounts/{account_id}/check-article-login")
async def check_article_login(account_id: int):
    """图文平台账号的登录态自检(纯 HTTP,不开浏览器)。

    浏览器系账号走 /api/accounts/{id}/refresh 那条;图文平台没有 storage_state
    可刷,只能拿 Cookie 打一次平台自身的接口来判活。
    """
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if acc.platform not in ARTICLE_PLATFORM_KEYS:
            raise HTTPException(400, f"{PLATFORM_SPEC(acc.platform).label} 不是图文平台")
        platform, cookie = acc.platform, acc.cookie or ""
        proxy, ua = acc.proxy or "", acc.ua or ""
    if not cookie:
        raise HTTPException(400, "该账号没有 Cookie,请重新用「Cookie 登录」导入")

    from application.base import AuthExpired, PlatformError
    session_cls, kwargs = _article_session(platform, cookie)
    try:
        async with session_cls(proxy=proxy, ua=ua, account_id=account_id,
                               **kwargs) as api:
            info = await api.check_login()
        status = "active"
        detail = info
    except AuthExpired as e:
        status, detail = "invalid", {"error": str(e)}
    except PlatformError as e:
        status, detail = "error", {"error": str(e)}
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if acc:
            if status == "active" and detail.get("nickname"):
                acc.nickname = detail["nickname"]
            acc.status = "active" if status == "active" else (
                "invalid" if status == "invalid" else acc.status)
            s.add(acc); s.commit()
    return {"status": status, **detail}


def _article_session(platform: str, cookie: str):
    """按平台挑协议客户端。公众号的凭证是 "cookie||token" 两样,这里拆开。"""
    from application.baijiahao import BaijiahaoSession
    from application.toutiao import ToutiaoSession
    from application.wechat_mp import WechatMpSession, split_creds
    if platform == "baijiahao":
        return BaijiahaoSession, {"cookie": cookie}
    if platform == "toutiao":
        return ToutiaoSession, {"cookie": cookie}
    ck, tk = split_creds(cookie)
    return WechatMpSession, {"cookie": ck, "token": tk}


@router.post("/api/accounts/{account_id}/relogin/start")
async def relogin_start(account_id: int):
    """重新登录:更新原账号的登录态(账号是创作者号则走创作中心)。"""
    if rt.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        is_creator = bool(acc.creator_storage_state)
        platform = acc.platform
        # 重登会清空旧认证 Cookie；完成扫码校验前账号不应继续显示“正常”。
        acc.status = "invalid"
        s.add(acc)
        s.commit()
    task_id = uuid.uuid4().hex
    rt.login_tasks[task_id] = {"status": "opening"}
    asyncio.create_task(_run_login(task_id, creator=is_creator, account_id=account_id,
                                   platform=platform))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开浏览器窗口,请扫码重新登录该账号"}
