"""本账号作品列表抓取(抖音/小红书/快手/视频号)。

2026-08-17 从 account_hub.py(1930 行)按功能域拆出。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin
from application.browser.fetcher import fetch_videos
from application.browser.ks_fetcher import fetch_ks_videos, fetch_ks_self_profile
from application.browser.channels_fetcher import fetch_channels_works
from application.browser.manager import BrowserManager
from application.browser.xhs_fetcher import fetch_xhs_notes
from application.kuaishou import parse_self_user as parse_ks_self_user
from contextlib import suppress
from application.browser.account_hub._shared import log


def _num(v) -> int:
    """互动数可能是 int / "1.2万" / "999+" 字符串,尽量转 int。"""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip().replace("+", "").replace(",", "")
    if not s:
        return 0
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100000000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = (d or {}).get(k)
        if v not in (None, "", [], {}):
            return v
    return default


_SELF_HOME = {
    "xhs": "https://www.xiaohongshu.com/explore",
    "kuaishou": "https://www.kuaishou.com/",
}


_SELF_LINK_JS = {
    # 顶栏头像/用户区指向自己主页(正文卡片作者链接太多,只认头部区域)
    "kuaishou": """() => {
        for (const sel of ['header a[href*="/profile/"]',
                           '[class*="header"] a[href*="/profile/"]',
                           '[class*="user-info"] a[href*="/profile/"]']) {
            const a = document.querySelector(sel);
            if (a) return a.href;
        }
        return '';
    }""",
}


async def _self_profile_link(mgr: BrowserManager, identity, platform: str
                             ) -> Tuple[str, str]:
    """打开站内首页,从「我」入口拿自己主页的真实链接(带 xsec_token 等参数)。
    返回 (绝对 URL, uid);拿不到返回 ("", "")。"""
    home = _SELF_HOME.get(platform)
    js = _SELF_LINK_JS.get(platform)
    if not home or (platform != "xhs" and not js):
        return "", ""
    href = ""
    if platform == "xhs":
        try:
            async with mgr.visible_page(identity) as page:
                await page.goto(
                    home, wait_until="domcontentloaded", timeout=30000)
                for _ in range(6):
                    anchors = page.locator('a[href*="/user/profile/"]')
                    count = min(await anchors.count(), 24)
                    sole = ""
                    for index in range(count):
                        anchor = anchors.nth(index)
                        if not await anchor.is_visible():
                            continue
                        candidate = urljoin(
                            home, await anchor.get_attribute("href") or "")
                        if count == 1:
                            sole = candidate
                        text = (await anchor.inner_text()).strip()
                        if text == "我":
                            href = candidate
                            break
                    href = href or sole
                    if href:
                        break
                    await mgr.xhs_interaction.pause(0.25, 0.55)
        except Exception:
            href = ""
        m = re.search(r"/user/profile/([0-9a-zA-Z_-]+)", href)
        uid = m.group(1) if m else ""
        log.debug(f"[hub-self] platform={platform} href={href!r} uid={uid}")
        return href, uid

    page = await mgr.new_page(identity, block_media=True)
    try:
        await page.goto(home, wait_until="domcontentloaded", timeout=30000)
        for _ in range(6):          # 侧栏/顶栏异步渲染,轮询最多 ~6s
            await page.wait_for_timeout(1000)
            try:
                href = await page.evaluate(js) or ""
            except Exception:
                href = ""
            if href:
                break
    except Exception:
        href = ""
    finally:
        with suppress(Exception):
            await page.close()
    m = re.search(r"/(?:user/profile|profile)/([0-9a-zA-Z_-]+)", href)
    uid = m.group(1) if m else ""
    log.debug(f"[hub-self] platform={platform} href={href!r} uid={uid}")
    return href, uid


def _norm_douyin_work(it: dict) -> Optional[dict]:
    aid = str(it.get("aweme_id") or "")
    if not aid:
        return None
    stats = it.get("statistics") or {}
    cover = ((it.get("video") or {}).get("cover") or {}).get("url_list") or []
    return {
        "item_id": aid,
        "desc": (it.get("desc") or "").strip(),
        "media_type": "images" if it.get("images") else "video",
        "cover_url": cover[0] if cover else "",
        "create_time": int(it.get("create_time") or 0),
        "like_count": _num(stats.get("digg_count")),
        "comment_count": _num(stats.get("comment_count")),
        "collect_count": _num(stats.get("collect_count")),
        "share_count": _num(stats.get("share_count")),
        "play_count": _num(stats.get("play_count")),
        "status": "",
    }


def _norm_xhs_work(it: dict) -> Optional[dict]:
    card = it.get("note_card") or it
    nid = str(_first(it, "note_id", "id", default="")
              or _first(card, "note_id", "id", default=""))
    if not nid:
        return None
    cover = card.get("cover") or {}
    cov = _first(cover, "url_default", "url_pre", "url", default="")
    if not cov and isinstance(cover.get("info_list"), list) and cover["info_list"]:
        cov = cover["info_list"][0].get("url", "")
    inter = card.get("interact_info") or {}
    return {
        "item_id": nid,
        "desc": _first(card, "display_title", "title", "desc", default=""),
        "media_type": "video" if card.get("type") == "video" else "images",
        "cover_url": cov,
        "create_time": int(_num(card.get("time")) / 1000) if _num(card.get("time")) > 1e12
                       else _num(card.get("time")),
        "like_count": _num(inter.get("liked_count")),
        "comment_count": _num(inter.get("comment_count")),
        "collect_count": _num(inter.get("collected_count")),
        "share_count": _num(inter.get("share_count")),
        "play_count": 0,
        "status": "",
        # 抓评论/打开笔记要用;SSR 项在 item 顶层,拦截项也可能带
        "xsec_token": str(_first(it, "xsec_token", "xsecToken", default="")
                          or _first(card, "xsec_token", "xsecToken", default="")),
    }


def _norm_ks_work(feed: dict) -> Optional[dict]:
    photo = (feed.get("photo") or {}) if isinstance(feed, dict) else {}
    pid = str(_first(photo, "id", default="") or feed.get("id") or "")
    if not pid:
        return None
    ts = _num(photo.get("timestamp"))
    return {
        "item_id": pid,
        "desc": (_first(photo, "caption", "name", default="") or "").strip(),
        "media_type": "images" if photo.get("atlas") or photo.get("imgUrls") else "video",
        "cover_url": _first(photo, "coverUrl", "cover_url", "webpCoverUrl", default=""),
        "create_time": int(ts / 1000) if ts > 1e12 else ts,
        "like_count": _num(_first(photo, "realLikeCount", "likeCount", default=0)),
        "comment_count": _num(photo.get("commentCount")),
        "collect_count": 0,
        "share_count": _num(photo.get("shareCount")),
        "play_count": _num(_first(photo, "viewCount", "playCount", default=0)),
        "status": "",
    }


def _norm_channels_work(it: dict) -> Optional[dict]:
    """视频号助手 post_list 一项 -> 本账号作品 dict。视频号视频加密不可下载,
    这里只记元数据+统计(供本账号作品展示 + 作品健康监控)。字段以真机抓包为准。"""
    if not isinstance(it, dict):
        return None
    oid = str(_first(it, "objectId", "exportId", "id", default="") or "")
    if not oid:
        return None
    # 助手当前 post_list 使用 desc；旧版/部分接口使用 objectDesc。
    od = _first(it, "objectDesc", "desc", default={}) or {}
    if not isinstance(od, dict):
        od = {}
    media = (od.get("media") or it.get("media") or [])
    m0 = media[0] if media and isinstance(media[0], dict) else {}
    fmt = str(_first(m0, "fileFormat", "mediaType", default="")).lower()
    media_type_code = _first(od, "mediaType", default=None)
    ts = _num(_first(it, "createtime", "createTime", default=0))
    description = (_first(od, "description", "shortTitle", default="")
                   or _first(it, "title", "description", default="") or "")
    if not isinstance(description, str):
        description = str(description)
    return {
        "item_id": oid,
        "desc": description.strip(),
        "media_type": ("images"
                       if media_type_code == 2 or fmt == "2"
                       or "pic" in fmt or "image" in fmt
                       else "video"),
        "cover_url": _first(m0, "coverUrl", "thumbUrl", default="")
                     or _first(it, "coverUrl", default=""),
        "create_time": int(ts / 1000) if ts > 1e12 else ts,
        "like_count": _num(_first(it, "likeCount", "like_count", default=0)),
        "comment_count": _num(_first(it, "commentCount", "comment_count", default=0)),
        "collect_count": _num(_first(it, "favCount", "collectCount", default=0)),
        "share_count": _num(_first(it, "forwardCount", "shareCount", default=0)),
        "play_count": _num(_first(it, "readCount", "playCount", "viewCount", default=0)),
        "status": str(_first(it, "statusText", "auditStatus", default="") or ""),
    }


#: 快手 `photoStatus` 可见性映射。2026-08-19 本机双判据实证:
#: ① www 主页抓到的 13 条**全部**落在 photoStatus=0(13/13);
#: ② photoStatus=0 共 14 条 = 资料页 `aweme_count`;
#: photoStatus=1 那 22 条主页流里一条都刷不到 ⇒ 0=公开、1=不公开。
_KS_PHOTO_STATUS = {0: "公开", 1: "不公开"}


def _norm_ks_cp_work(it: dict) -> Optional[dict]:
    """创作平台 `home/photo/list` 一项 -> 本账号作品 dict。
    `collect_count`/`share_count` 该接口不给,留 0(**不是 0 收藏,是没这个字段**)。"""
    if not isinstance(it, dict):
        return None
    wid = str(it.get("workId") or "")
    if not wid:
        return None
    ts = _num(it.get("uploadTime"))
    ps = it.get("photoStatus")
    return {
        "item_id": wid,
        "desc": str(it.get("title") or "").strip(),
        "media_type": "images" if it.get("showAtlasIcon") else "video",
        "cover_url": str(it.get("publishCoverUrl") or ""),
        "create_time": int(ts / 1000) if ts > 1e12 else ts,
        "like_count": _num(it.get("likeCount")),
        "comment_count": _num(it.get("commentCount")),
        "collect_count": 0,
        "share_count": 0,
        "play_count": _num(it.get("playCount")),
        "status": _KS_PHOTO_STATUS.get(ps, f"photoStatus={ps}"),
        "raw_json": json.dumps(it, ensure_ascii=False),
    }


async def fetch_account_works(mgr: BrowserManager, identity, platform: str, uid: str,
                              max_scrolls: int = 14,
                              cp_cookies: Optional[Dict[str, str]] = None
                              ) -> Tuple[List[dict], str]:
    """抓取登录账号自己发布的作品(复用各平台已有的主页拦截抓取)。
    返回 (归一后的作品 dict 列表, error)。需要账号已知自身 uid/sec_uid。
    入参用 identity/platform/uid 原语(由调用方在 session 活跃时取出),避免 ORM 实例失效。

    快手额外走一条 **cp 优先、www 兜底**:`cp_cookies` 给了就先打创作平台的
    作品列表 —— 它不需要 www 站点会话、不需要 3x uid,还能看到不公开作品。
    只登了创作平台的账号(www 侧恒 `no_profile_data`)只有这条路走得通。"""
    uid = (uid or "").strip()
    open_url = ""
    #: ── 快手:先试创作平台(零浏览器、零签名)──────────────────────────
    #: 判据是 `kuaishou.web.cp.api_ph` 在不在,**不是 dict 空不空** ——
    #: 只登了主站的账号(如本机 acct2)照样有一堆快手 cookie,dict 非空但没有
    #: cp 令牌,那样会白跑一趟 cp 再回落、每次同步刷一条无谓的 warning。
    if platform == "kuaishou" and (cp_cookies or {}).get("kuaishou.web.cp.api_ph"):
        #: 函数内导入:`application.kuaishou` 与 `application.browser` 之间
        #: 本来就有一圈循环依赖(kuaishou/__init__ → extract → douyin →
        #: browser → 本模块 → kuaishou),模块级再加一条会让「谁先被 import」
        #: 决定成败。放进函数里,导入顺序就不重要了。
        from application.kuaishou.api import fetch_cp_photo_list
        cp_items, cp_err = await fetch_cp_photo_list(
            cp_cookies, ua=getattr(identity, "ua", "") or "",
            proxy=getattr(identity, "proxy", "") or None, limit=100)
        cp_out = [w for w in (_norm_ks_cp_work(it) for it in (cp_items or [])) if w]
        if cp_out:
            return cp_out, ""
        #: 不 return —— cp 不通就照旧回落主站,别把已经能用的 www 路径改坏
        log.warning("[hub-self] kuaishou cp 取数未果(%s),回落主站", cp_err)
    if platform == "xhs":
        # 小红书:站内「我」入口拿真实主页链接(带 xsec_token);失败再退回 uid 直开
        open_url, self_uid = await _self_profile_link(mgr, identity, "xhs")
        uid = uid or self_uid
    elif platform == "kuaishou" and (not uid or uid.isdigit()):
        # 快手:header 抓链接不稳(实测 href='');/profile 只认真实 3x id(纯数字 userId 会 404)。
        # 改用可靠法 —— cookie 数字 userId 反查本人 3x id(与「刷新资料」同一套 fetch_ks_self_profile)
        try:
            prof, perr = await fetch_ks_self_profile(mgr, identity)
            if perr == "logged_out":
                return [], "logged_out:登录态失效,请重新登录"
            self3x = str(parse_ks_self_user(prof or {}).get("sec_uid") or "")
            if self3x:
                uid = self3x
        except Exception as e:
            log.warning(f"[hub-self] kuaishou self-resolve failed: {e!r}")
    # 视频号:助手接口即本账号,不需要 uid
    if not uid and not open_url and platform != "shipinhao":
        return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」再同步作品"

    known: Set[str] = set()
    try:
        if platform == "shipinhao":
            items, _author, err = await fetch_channels_works(mgr, identity, known,
                                                             max_scrolls=max_scrolls)
            norm = _norm_channels_work
        elif platform == "xhs":
            items, _author, err = await fetch_xhs_notes(mgr, identity, uid, known,
                                                        xsec_token="",
                                                        max_scrolls=max_scrolls,
                                                        open_url=open_url,
                                                        ssr_fallback=True)
            norm = _norm_xhs_work
        elif platform == "kuaishou":
            items, _author, err = await fetch_ks_videos(mgr, identity, uid, known,
                                                        max_scrolls=max_scrolls,
                                                        open_url=open_url)
            norm = _norm_ks_work
        else:
            items, _author, err = await fetch_videos(mgr, identity, uid, known,
                                                     max_scrolls=max_scrolls)
            norm = _norm_douyin_work
    except Exception as e:
        return [], f"抓取作品异常: {e!r}"

    out = [w for w in (norm(it) for it in (items or [])) if w]
    return out, ("" if out else err)
