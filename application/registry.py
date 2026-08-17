"""平台注册表:全项目唯一的平台清单与能力声明。

历史问题:平台名硬编码散在 main.py(97 处)/account_hub.py(96 处)/monitor.py(60 处)
等 300+ 处,每加一个平台要手工铺 ~21 处分支(视频号实测)。本表把「加平台必碰的
数据型差异」(域名/首页/中文名/能力开关)收敛到一处;函数型差异(怎么发布/怎么抓)
仍由各平台包实现,由调用方按 spec 的能力字段分发。

新增平台的步骤:
  1. 在下面 SPECS 加一条 PlatformSpec
  2. 建 application/<key>/ 包,按能力实现 publish/extract 等
  3. 在 engine/publishing.py 的发布分发处按 publish_via 接一条(纯 HTTP 平台走 article 通道)
前端(app/static/app.js)的平台按钮与主题色仍需手工加——它不 import Python。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlatformSpec:
    key: str                      # 平台标识(库里 platform 列的值)
    label: str                    # 中文名(界面/通知用)
    host: str                     # 主域名(open-browser 的 URL 白名单校验)
    home_url: str                 # 平台首页/后台入口
    cookie_domain: str            # 注入 Cookie 的默认 domain
    kind: str = "video"           # video=视频/图集平台 | article=图文文章平台
    account_label: str = ""       # 登录任务里的账号称呼(空=「<label>账号」)
    publish_via: str = "browser"  # browser=有头浏览器自动化 | http=纯协议 | none
    can_monitor_others: bool = True   # 能否监控他人作品/评论(视频号/图文平台不能)
    login_methods: tuple = ("qr",)    # qr=扫码 | cookie=粘贴 Cookie | creator=创作者登录 | browser=开后台登录页自动抓 Cookie
    publish_note: str = ""        # 发布语义备注(如公众号只能到草稿)

    @property
    def display_account(self) -> str:
        return self.account_label or f"{self.label}账号"


SPECS: dict[str, PlatformSpec] = {s.key: s for s in [
    PlatformSpec(
        key="douyin", label="抖音", host="douyin.com",
        home_url="https://www.douyin.com/", cookie_domain=".douyin.com",
        login_methods=("qr", "cookie", "creator")),
    PlatformSpec(
        key="xhs", label="小红书", host="xiaohongshu.com",
        home_url="https://www.xiaohongshu.com/", cookie_domain=".xiaohongshu.com",
        login_methods=("qr", "cookie", "creator")),
    PlatformSpec(
        key="kuaishou", label="快手", host="kuaishou.com",
        home_url="https://www.kuaishou.com/", cookie_domain=".kuaishou.com",
        login_methods=("qr", "cookie", "creator")),
    PlatformSpec(
        key="shipinhao", label="视频号", host="weixin.qq.com",
        home_url="https://channels.weixin.qq.com/platform",
        cookie_domain=".weixin.qq.com",
        can_monitor_others=False, login_methods=("qr",)),
    # ── 图文文章平台(2026-08-17 起,纯 HTTP 发布,协议移植自 quote-video 实战线)──
    PlatformSpec(
        key="baijiahao", label="百家号", host="baijiahao.baidu.com",
        home_url="https://baijiahao.baidu.com/builder/rc/home",
        cookie_domain=".baidu.com", kind="article", publish_via="http",
        can_monitor_others=False, login_methods=("cookie", "browser")),
    PlatformSpec(
        key="toutiao", label="今日头条", host="mp.toutiao.com",
        home_url="https://mp.toutiao.com/profile_v4/index",
        cookie_domain=".toutiao.com", kind="article", publish_via="http",
        can_monitor_others=False, login_methods=("cookie", "browser")),
    PlatformSpec(
        key="wechat_mp", label="微信公众号", host="mp.weixin.qq.com",
        home_url="https://mp.weixin.qq.com/",
        cookie_domain="mp.weixin.qq.com", kind="article", publish_via="http",
        can_monitor_others=False, login_methods=("cookie", "browser"),
        publish_note="未认证订阅号群发需扫码,自动线只到草稿箱(建成即视为完成)"),
    PlatformSpec(
        key="weibo", label="微博", host="weibo.com",
        home_url="https://weibo.com/", cookie_domain=".weibo.com",
        kind="article", publish_via="none",
        can_monitor_others=False, login_methods=("cookie", "browser"),
        publish_note="仅登录态管理:产线热点线抓 s.weibo.com 靠它供 Cookie,不做发布"),
]}

# 常用派生集合(调用方别再手写平台元组)
ALL_KEYS = tuple(SPECS)
VIDEO_KEYS = tuple(k for k, s in SPECS.items() if s.kind == "video")
ARTICLE_KEYS = tuple(k for k, s in SPECS.items() if s.kind == "article")
MONITORABLE_KEYS = tuple(k for k, s in SPECS.items() if s.can_monitor_others)
COOKIE_LOGIN_KEYS = tuple(k for k, s in SPECS.items() if "cookie" in s.login_methods)


def spec(platform: str) -> PlatformSpec:
    """取平台声明;未知平台回落抖音(与历史行为一致,不抛错)。"""
    return SPECS.get(platform) or SPECS["douyin"]


def normalize(platform: str | None, default: str = "douyin",
              allowed: tuple = ALL_KEYS) -> str:
    """平台参数清洗:替代散落各处的 `x if x in (...) else "douyin"`。"""
    return platform if platform in allowed else default


def label(platform: str) -> str:
    return spec(platform).label


def article_session(platform: str, credentials: str):
    """图文平台协议客户端工厂,返回 (session_cls, init_kwargs)。

    公众号凭证是 "cookie||token" 合并形态,在此拆开。惰性 import 防循环
    (平台包自身 import 本注册表)。app 层与 engine 体检共用这一个入口。"""
    if platform == "baijiahao":
        from application.baijiahao import BaijiahaoSession
        return BaijiahaoSession, {"cookie": credentials}
    if platform == "toutiao":
        from application.toutiao import ToutiaoSession
        return ToutiaoSession, {"cookie": credentials}
    if platform == "weibo":
        from application.weibo import WeiboSession
        return WeiboSession, {"cookie": credentials}
    from application.wechat_mp import WechatMpSession, split_creds
    ck, tk = split_creds(credentials)
    return WechatMpSession, {"cookie": ck, "token": tk}
