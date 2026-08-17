"""微博协议客户端:判活 + 凭证寿命。

只做登录态管理,不做发布(registry 里 publish_via="none")。
判活走 web 版全局配置接口 /ajax/config——免签名,未登录时 login=false,
比打业务接口稳。ALF 是微博 Cookie 的自动登录过期时间戳(实测服务端
**不滑动续期**,见 quote-video hot_article 线 2026-08-09 标定),这里
顺带算出剩余天数,方便面板/体检提前预警。
"""
from __future__ import annotations

import time

from application.base import ArticlePlatformSession, AuthExpired

WB = "https://weibo.com"


class WeiboSession(ArticlePlatformSession):
    PLATFORM = "weibo"

    def _headers(self, referer: str = WB + "/") -> dict:
        return self.base_headers(Referer=referer,
                                 **{"X-Requested-With": "XMLHttpRequest"})

    def cookie_days_left(self) -> float | None:
        """ALF(Auto-Login-Fail)时间戳 → 剩余天数;没配或解析不出返回 None。"""
        alf = self.parse_cookie(self.cookie).get("ALF", "")
        if not alf.isdigit():
            return None
        return round((int(alf) - time.time()) / 86400, 1)

    async def check_login(self) -> dict:
        """有效返回 {uid, nickname, days_left};失效抛 AuthExpired。"""
        r = await self.get(WB + "/ajax/config", headers=self._headers())
        d = (await self.json_or_raise(r, "config")).get("data") or {}
        if not d.get("login"):
            raise AuthExpired("微博登录态已失效,请重新登录")
        uid = str(d.get("uid") or "")
        nickname = ""
        try:
            pr = await self.get(WB + f"/ajax/profile/info?uid={uid}",
                                headers=self._headers())
            user = ((await self.json_or_raise(pr, "profile")).get("data")
                    or {}).get("user") or {}
            nickname = user.get("screen_name") or ""
        except Exception:
            pass                     # 昵称拿不到不算失败,uid 已证明登录态有效
        out = {"uid": uid, "nickname": nickname}
        days = self.cookie_days_left()
        if days is not None:
            out["days_left"] = days
        return out
