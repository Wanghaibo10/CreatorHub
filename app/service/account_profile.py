"""账号资料抓取与入库(登录后 / 手动刷新共用)。

从 main.py 抽出(2026-08-17 模块化):login 与 accounts 两个 router 共用。
"""
from __future__ import annotations

import asyncio

from application.browser import (fetch_channels_self_profile, fetch_ks_self_profile, fetch_self_profile)
from moss.core.config import get_config
from moss.common.db import get_session
from moss.common.logging_setup import get_logger
from moss.model import DouyinAccount
from application.channels import parse_self_user as parse_channels_self_user
from application.douyin import parse_self_user
from application.kuaishou import parse_self_user as parse_ks_self_user
from application.xhs import (XhsApiClient, XhsApiError, cookie_str_from_state, has_a1, parse_self_user as parse_xhs_self_user)
from moss.core.risk import RiskCategory, classify_platform_error
from moss.core.runtime import rt

log = get_logger("services.account_profile")
cfg = get_config()


async def _xhs_profile(state: str, proxy: str = "", *, detailed: bool = False):
    """用签名直连 API 拿小红书账号资料(me 身份 + otherinfo 昵称/头像/粉丝)。
    返回 (user dict, error)。error == "logged_out" 表示登录态失效。"""
    cookie_str = cookie_str_from_state(state)
    if not has_a1(cookie_str):
        return {}, "logged_out"
    client = XhsApiClient(cookie_str, cfg.engine.user_agent,
                          timeout=cfg.engine.request_timeout_seconds, proxy=proxy)
    try:
        me = await client.self_info()
    except XhsApiError as exc:
        if detailed:
            return {}, exc
        return {}, "logged_out" if exc.category == "auth" else exc.category
    except Exception as e:
        log.warning(f"self_info 失败: {e!r}")
        return ({}, e) if detailed else ({}, "error")
    if not me or me.get("guest") is True or not me.get("user_id"):
        return {}, "logged_out"
    merged = dict(me)
    try:
        other = await client.user_info(me["user_id"])
        if other:
            merged = {**other, **me}      # me 提供身份,otherinfo 提供 basic_info/粉丝
    except Exception as exc:
        category, _signal = classify_platform_error(exc)
        if detailed and category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
            return {}, exc
    return merged, ""


async def _fetch_channels_profile_with_retry(identity, attempts: int = 3) -> tuple[dict, str]:
    """视频号扫码后的会话传播窗口内重试，避免一次跳登录页就误判失效。"""
    result: tuple[dict, str] = ({}, "logged_out")
    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(1.5 * attempt)
        result = await fetch_channels_self_profile(rt.browser, identity)
        profile, error = result
        if profile or error != "logged_out":
            break
    return result


async def _enrich_account_profile(account_id: int, state: str, *,
                                  detailed: bool = False):
    """用登录态拉取账号资料；默认返回旧的状态字符串，详细模式附带原始错误。"""
    def _done(status: str, error=""):
        return (status, error) if detailed else status

    if rt.browser is None or not state:
        return _done("error", "error")
    with get_session() as s:
        a0 = s.get(DouyinAccount, account_id)
        platform = a0.platform if a0 else "douyin"
        creator_state = a0.creator_storage_state if a0 else ""
        proxy = (a0.proxy or "") if a0 else ""
        identity = rt.browser.identity_for(a0) if a0 else rt.browser.anon_identity()

    # XHS 创作者号:用创作平台「我的信息」拿资料 + 判活(www 接口对创作态拿不到)
    if platform == "xhs" and creator_state:
        from application.xhs import creator_profile, creator_check
        profile_error = ""
        if detailed:
            prof, profile_error = await creator_profile(
                creator_state, proxy=proxy, preserve_error=True)
        else:
            prof = await creator_profile(creator_state, proxy=proxy)
        if prof and (prof.get("nickname") or prof.get("douyin_id")):
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if acc:
                    if prof.get("nickname"):
                        acc.nickname = prof["nickname"]
                    acc.sec_uid = prof.get("sec_uid") or acc.sec_uid
                    acc.douyin_id = prof.get("douyin_id") or acc.douyin_id
                    acc.avatar = prof.get("avatar") or acc.avatar
                    acc.follower_count = prof.get("follower_count") or acc.follower_count
                    acc.aweme_count = prof.get("aweme_count") or acc.aweme_count
                    acc.status = "active"
                    s.add(acc); s.commit()
            return _done("ok")
        if profile_error:
            category, _signal = classify_platform_error(profile_error)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                status = "invalid" if category == RiskCategory.AUTH else "error"
                return _done(status, profile_error)
        check_error = ""
        if detailed:
            chk, check_error = await creator_check(
                creator_state, proxy=proxy, preserve_error=True)
        else:
            chk = await creator_check(creator_state, proxy=proxy)
        if chk is True:
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if acc:
                    acc.status = "active"
                    s.add(acc); s.commit()
            return _done("ok")
        if check_error:
            category, _signal = classify_platform_error(check_error)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                status = "invalid" if category == RiskCategory.AUTH else "error"
                return _done(status, check_error)
        if chk is None:
            return _done("error", check_error or profile_error or "error")
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if acc:
                acc.status = "invalid"
                s.add(acc); s.commit()
        return _done("invalid", check_error or "logged_out")

    try:
        if platform == "xhs":
            u, err = await _xhs_profile(state, proxy, detailed=detailed)
        elif platform == "kuaishou":
            u, err = await fetch_ks_self_profile(rt.browser, identity)
        elif platform == "shipinhao":
            # 视频号扫码授权后，服务端会话偶尔要数秒才在新页面中生效。
            # 单次打开被重定向到登录页不能立即把刚添加的账号判为失效。
            u, err = await _fetch_channels_profile_with_retry(identity)
        else:
            u, err = await fetch_self_profile(rt.browser, identity)
    except Exception as exc:
        category, _signal = classify_platform_error(exc)
        status = "invalid" if detailed and category == RiskCategory.AUTH else "error"
        return _done(status, exc)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            return _done("error", "error")
        if u:
            if platform == "xhs":
                p = parse_xhs_self_user(u)
            elif platform == "kuaishou":
                p = parse_ks_self_user(u)
            elif platform == "shipinhao":
                p = parse_channels_self_user(u)
            else:
                p = parse_self_user(u)
            if p.get("nickname"):
                acc.nickname = p["nickname"]
            acc.sec_uid = p.get("sec_uid") or acc.sec_uid
            acc.douyin_id = p.get("douyin_id") or acc.douyin_id
            acc.avatar = p.get("avatar") or acc.avatar
            acc.follower_count = p.get("follower_count") or acc.follower_count
            acc.aweme_count = p.get("aweme_count") or acc.aweme_count
            acc.status = "active"
            s.add(acc); s.commit()
            return _done("ok")
        if (err == "logged_out" or
                getattr(err, "category", None) == RiskCategory.AUTH.value):
            acc.status = "invalid"
            s.add(acc); s.commit()
            return _done("invalid", err or "logged_out")
    return _done("error", err or "error")
