"""各平台「纯协议发包」的公共基类。

为什么要这层:图文平台(百家号/头条/公众号)全部走纯 HTTP,不开浏览器,
所以拿不到 BrowserManager 那套 identity/storage_state。它们共同需要的是:
  · 真实 Chrome TLS 指纹(百家号的 WAF 认 JA3,urllib 会被挡)
  · 账号级 Cookie + 独立代理(沿用 DouyinAccount 上已有的隔离画像)
  · 失败重试 + 把平台错误翻译成 RiskCategory,好接进既有风控闸门

发包能力直接复用 app/common/requests_extension/async_session.py(curl_cffi 异步基座),
本模块只补 CreatorHub 侧的凭证与风控接线。

平台子类要做的事:
  1. 覆盖 PLATFORM(注册表 key)
  2. 覆盖 check_login() —— 校验登录态并返回账号信息
  3. 覆盖 publish_article() —— 发一篇图文,返回 ArticleResult
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from moss.common.requests_extension.async_session import AsyncMCurlSession
from moss.core.risk import RiskCategory
from application.registry import spec

logger = logging.getLogger("creatorhub.platforms")


class PlatformError(Exception):
    """带风控分类的平台错误。

    classify_platform_error() 会优先读 .category/.signal/.status_code 三个属性,
    所以这里挂上去,既有的风控闸门无需改动就能识别图文平台的错误。

    ⚠️ category 默认 None 而不是 BUSINESS:一旦挂上非空 .category,
    classify_platform_error() 就走「显式分类」分支直接返回,不再看 status_code——
    那样 `PlatformError(..., status_code=429)` 会被判成 business 而漏掉风控冷却。
    不确定归类时就别设 category,把 status_code 交给既有规则去判。
    """

    def __init__(self, message: str, *, category: RiskCategory | None = None,
                 signal: str = "", status_code: int | None = None):
        super().__init__(message)
        if category is not None:
            self.category = category
        if signal or category is not None:
            self.signal = signal or (category.value if category else "")
        self.status_code = status_code


class AuthExpired(PlatformError):
    """登录态失效。单独一个类型,因为调用方要据此把账号标 invalid 而不是重试。"""

    def __init__(self, message: str = "登录态已失效,请重新登录"):
        super().__init__(message, category=RiskCategory.AUTH, signal="logged_out")


@dataclass
class ArticleDraft:
    """一篇待发布的图文。三个平台的输入差异都收敛到这里。

    markdown 里的本地图 `![图注](相对路径)` 由各平台自己上传换成图床地址,
    base_dir 是解析相对路径的基准目录。
    """
    title: str
    markdown: str = ""
    content_html: str = ""          # 直接给 HTML(与 markdown 二选一)
    base_dir: Path = field(default_factory=Path.cwd)
    covers: list[Path] = field(default_factory=list)
    digest: str = ""                # 摘要(公众号用,留空自动取正文开头)
    author: str = ""
    topics: list[str] = field(default_factory=list)
    ai_generated: bool = True       # 平台的 AIGC 声明开关,管线产出默认要声明
    extras: dict[str, Any] = field(default_factory=dict)   # 平台专属开关


@dataclass
class ArticleResult:
    ok: bool
    url: str = ""
    article_id: str = ""
    is_draft: bool = False          # 只到草稿(公众号)
    message: str = ""
    raw: dict = field(default_factory=dict)


class ArticlePlatformSession(AsyncMCurlSession):
    """图文平台协议客户端基类。

    用法:
        async with BaijiahaoSession(cookie=..., proxy=...) as api:
            await api.check_login()
            result = await api.publish_article(draft, publish=True)
    """

    PLATFORM = ""                   # 子类覆盖:注册表里的 key

    def __init__(self, cookie: str = "", *, proxy: str = "", ua: str = "",
                 account_id: int | None = None, retries: int = 2,
                 backoff_factor: float = 1.5, **kwargs):
        self.cookie = (cookie or "").strip()
        self.account_id = account_id
        self._ua = ua or self.DEFAULT_UA
        proxies = {"http": proxy, "https": proxy} if proxy else None
        super().__init__(
            session_id=f"{self.PLATFORM or 'platform'}#{account_id or '-'}",
            proxies_refresh=(lambda *_a, **_k: proxies) if proxies else None,
            logger=logger, retries=retries, backoff_factor=backoff_factor,
            impersonate="chrome", **kwargs)

    DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

    @property
    def label(self) -> str:
        return spec(self.PLATFORM).label

    def base_headers(self, **extra) -> dict:
        h = {"User-Agent": self._ua, "Accept": "application/json, text/plain, */*",
             "Accept-Language": "zh-CN,zh;q=0.9"}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(extra)
        return h

    async def response_callback(self, response, method: str, url: str, **kwargs):
        """统一把 HTTP 层的风控信号翻成 PlatformError,不吞不改响应体。

        403/429 这类由 risk.py 的既有梯度冷却接管;401 归 AUTH,让调用方去标账号失效。
        """
        code = response.status_code
        if code in (401,):
            raise AuthExpired(f"{self.label} 返回 401")
        if code in (403, 429, 461, 471):
            raise PlatformError(f"{self.label} 返回 {code}(疑似风控)",
                                category=RiskCategory.RISK, signal=f"http_{code}",
                                status_code=code)
        return response

    async def request_fail_callback(self, e: Exception, method: str, url: str, **kwargs):
        """基座对一切异常都会重试(retries 次)。PlatformError 一律不该重试:
        401 重试不会变成已登录;403/429 是风控信号,立刻再打同一接口只会加重,
        应交给 risk.py 的梯度冷却。这里直接 raise 终止基座的重试循环。"""
        if isinstance(e, PlatformError):
            raise e

    @asynccontextmanager
    async def no_retry(self):
        """发布这类非幂等 POST 用:超时后服务端可能已受理,盲目重发有重复发文风险
        (平台探测铁律:即时查不到≠没发成功)。会话是 per-任务的,改属性无并发共享。"""
        saved = self._retries
        self._retries = 0     # 循环条件 retry_count(0) >= 0 恒真 → 首次失败即抛
        try:
            yield
        finally:
            self._retries = saved

    @staticmethod
    def parse_cookie(raw: str) -> dict:
        out = {}
        for part in (raw or "").split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        return out

    async def json_or_raise(self, response, what: str) -> dict:
        """平台接口偶尔在风控/登录失效时回 HTML 登录页,这里统一拦成可读错误。"""
        text = response.text
        try:
            return json.loads(text)
        except Exception:
            snippet = text[:200].replace("\n", " ")
            if "login" in text[:2000].lower() or "登录" in text[:2000]:
                raise AuthExpired(f"{self.label} {what} 返回登录页,登录态已失效")
            raise PlatformError(
                f"{self.label} {what} 返回非 JSON(status={response.status_code}): {snippet}",
                status_code=response.status_code)

    # ── 子类实现 ───────────────────────────────────────────────────────────
    async def check_login(self) -> dict:
        """校验登录态。有效返回 {nickname, uid, ...};失效抛 AuthExpired。"""
        raise NotImplementedError

    async def publish_article(self, draft: ArticleDraft, *,
                              publish: bool = False) -> ArticleResult:
        """发一篇图文。publish=False 只存草稿(平台支持的话)。"""
        raise NotImplementedError


def markdown_images(md: str) -> list[tuple[str, str]]:
    """抽出 markdown 里的 (alt, src),三个平台的图片处理都要先做这步。"""
    import re
    return re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", md or "")


def resolve_local(src: str, base_dir: Path) -> Optional[Path]:
    """把 markdown 里的图片路径解析成本地文件;网络图返回 None。"""
    if src.startswith(("http://", "https://")):
        return None
    p = Path(src)
    return p if p.is_absolute() else (base_dir / src).resolve()
