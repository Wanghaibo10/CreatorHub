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
        """有效返回 {nickname, days_left};失效抛 AuthExpired。

        2026-08-17 实测标定:web 版 ajax 接口未登录一律回 {"ok":-100,跳
        login.php};带有效 Cookie 打 /ajax/profile/info(不带参)回 400
        「缺少必要参数」——**能走到参数校验就说明已过登录闸**。别用
        /ajax/config,那个路径 404。昵称暂无实证接口可取,留空。"""
        r = await self.get(WB + "/ajax/profile/info", headers=self._headers())
        d = await self.json_or_raise(r, "profile/info")
        if d.get("ok") == -100:
            raise AuthExpired("微博登录态已失效,请重新登录")
        out = {"nickname": ""}
        days = self.cookie_days_left()
        if days is not None:
            out["days_left"] = days
        return out
