"""抖音创作平台**纯协议**发视频 —— 零浏览器。

登录态从 CreatorHub sqlite 的 `storage_state` 读。上传走字节 VOD,
投稿走 creator.douyin.com。签名两套:

    a_bogus     creator 域 query(仓里 signing/abogus.py)
    AWS SigV4   vod.bytedanceapi.com(与 kv 剪映上传同形,region=sdwdmwlll)

════════ 协议(2026-08-17 半世清言浏览器实发 + 抓包对照)════════

    GET  /web/api/media/upload/auth/v5/          → STS              ✅
    GET  /aweme/mid/video/sts2/?scene=web        → 另一份 STS       ✅(浏览器也打)
    GET  vod ApplyUploadInner  app_id=2906       → 上传槽           ✅
    POST {UploadHost}/upload/v1/{StoreUri}       分片 + crc32       ✅
    POST vod CommitUploadInner                   → video_id         ✅
    GET  /web/api/media/video/enable/?video_id=  → 转码就绪         ✅
    POST /web/api/media/aweme/create_v2/         JSON 投稿
         query: aid=1128 + read_aid=2906
         body:  {item:{common,cover,mix,...}}    ✅ 200 + item_id
         旧路径 /aweme/create/ multipart 已弃,JSON 打它会 403。

2026-08-17 半世清言:浏览器公开条 + 纯协议私密条(item=7675011062814477583)都进过库。
2026-08-18 换号 create_v2 403 空 body,两处客户端洞:
  1) query 曾手写 browser_name=Chrome/131,抓包是 Mozilla + UA 去 Mozilla/ 前缀
  2) 用 passport_csrf_token 冒充 x-secsdk-csrf-token,且不做路径预检
现按抓包对齐指纹;POST 前 HEAD/GET 目标路径收 x-ware-csrf-token。
2026-08-18 二轮,又两处「自己跟自己打架」(httpbin 回显实测出来的):
  3) 库里 acc.ua 是 patchright 原始串,写着 HeadlessChrome —— 它同时进
     User-Agent 头与风控 query 的 browser_version。见 normalize_ua()
  4) curl_cffi impersonate 默认头是导航形(navigate/document/none/UIR=1),
     带 Origin 的 POST 配这套 = 跨站表单指纹。见 _creator_headers()
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from curl_cffi.requests import AsyncSession

from application.douyin.signing import gen_false_ms_token, sign_url
from application.douyin.signing import ticket_guard
from application.douyin import verify as _verify
from moss.common.netfp import impersonate_for_ua

CREATOR = "https://creator.douyin.com"
VOD_HOST = "vod.bytedanceapi.com"
#: 2024-03 公开链;STS 不带空间名,改了只动这一处
SPACE_NAME = "aweme"
#: creator 域 query 真源。2026-08-17 实发 create_v2 / user/info / work_list 都是 1128。
CREATOR_AID = "1128"
#: VOD / ImageX / create_v2 的 read_aid。浏览器 ApplyUploadInner 带 app_id=2906。
IMAGEX_APP_ID = "2906"
AID = CREATOR_AID
#: 与 kv 剪映 VOD 同一 region(签成 cn-north-1 会被拒)
VOD_REGION = "sdwdmwlll"
CHUNK = 4 * 1024 * 1024
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

#: visibility_type 是 JSON 数字,不是字符串。0 公开 / 1 私密 / 2 好友。
#: 2026-08-17 公开条 vis=0,探测条 vis=1,都是 create_v2 200。
VISIBILITY = {"public": 0, "friends": 2, "private": 1}


class DouyinAPIError(RuntimeError):
    pass


def cookies_from_account(acc) -> Dict[str, str]:
    """从 `storage_state` 抽抖音域 cookie,不启动浏览器。"""
    raw = getattr(acc, "storage_state", "") or getattr(acc, "creator_storage_state", "") or ""
    return cookies_from_state(raw)


def cookies_from_state(storage_state_json: str) -> Dict[str, str]:
    if not storage_state_json:
        return {}
    try:
        data = json.loads(storage_state_json)
    except json.JSONDecodeError:
        return {}
    out: Dict[str, str] = {}
    rows = [c for c in data.get("cookies") or []
            if any(k in (c.get("domain") or "")
                   for k in ("douyin", "bytedance", "amemv"))]
    for c in sorted(rows, key=lambda x: len(x.get("domain") or "")):
        name, val = c.get("name"), c.get("value")
        if name and val is not None:
            out[str(name)] = str(val)
    return out


def verify_required(headers: Any) -> bool:
    """响应是不是「要求本人身份验证」。

    形态:`HTTP 200` + **空 body** + `x-tt-verify-passport-decision`
    (内含 `account_flow: verify`)。2026-08-18 实证:真实浏览器首次发布也是
    这个响应,人过一次短信验证码后重发即成功 —— 所以它**不是协议错误**。
    单独识别出来,是为了别让上层看到「HTTP 200 空 body」就去改签名改指纹。
    """
    for k, v in (headers or {}).items():
        if k.lower() == "x-tt-verify-passport-decision" and v:
            return True
    return False


def _cookie_header(ck: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in ck.items())


def browser_query(ua: str, *, screen: tuple[int, int] = (1536, 864)
                  ) -> dict[str, str]:
    """创作域公共 query,对齐 2026-08-17 create_v2 抓包。

    浏览器发的是 navigator.appName / appVersion:
    browser_name=Mozilla, browser_version=<UA 去掉前缀 Mozilla/>。
    不是 Chrome/131.0.0.0。
    """
    ua = (ua or DEFAULT_UA).strip()
    version = ua[len("Mozilla/"):] if ua.startswith("Mozilla/") else ua
    if "Mac" in ua:
        plat = "MacIntel"
    elif "Windows" in ua or "Win64" in ua or "Win32" in ua:
        plat = "Win32"
    else:
        plat = "Linux x86_64"
    w, h = screen
    return {
        "cookie_enabled": "true",
        "screen_width": str(int(w)),
        "screen_height": str(int(h)),
        "browser_language": "zh-CN",
        "browser_platform": plat,
        "browser_name": "Mozilla",
        "browser_version": version,
        "browser_online": "true",
        "timezone_name": "Asia/Shanghai",
        "support_h265": "1",
    }


def normalize_ua(ua: str) -> str:
    """洗掉 UA 里的自动化痕迹,并把 Chrome 大版本对齐到 impersonate 目标。

    ⚠️ 两个洞,都是「自己跟自己打架」:

    1. `douyinaccount.ua` 存的是 patchright 启动时的**原始** UA,里面写着
       `HeadlessChrome/151.0.0.0`。`publish_cli.py` 直接 `acc.ua` 往下传,
       于是这串同时进 `User-Agent` 头**和** `browser_query()` 的
       `browser_version`(风控 query)—— 等于自报两次「我是无头浏览器」。
       2026-08-17 22:17 浏览器实发成功那条抓包里是 `Chrome/151.0.0.0`,
       没有 Headless(页面层被 patchright 洗过了),库里那份没洗。
    2. curl_cffi 只有有限几个 impersonate 目标,UA 写 Chrome/151 时它挑
       chrome146,`Sec-Ch-Ua` 就自报 v="146" —— 真 Chrome 的 UA 与
       Client Hints 永远一致,对不上就是个可判据的破绽。这里把 UA 的大版本
       改成 impersonate 目标的版本,让 UA / Sec-Ch-Ua / TLS 三者自洽。
    """
    ua = (ua or DEFAULT_UA).strip()
    ua = ua.replace("HeadlessChrome/", "Chrome/")
    m = re.search(r"chrome(\d+)", impersonate_for_ua(ua))
    if m:
        ua = re.sub(r"Chrome/\d+", "Chrome/" + m.group(1), ua)
    return ua


def parse_ware_csrf(header: str) -> str:
    """`x-ware-csrf-token: 0,<token>,<sign>` → 给 `x-secsdk-csrf-token` 的 token。

    不要拿 cookie 里的 passport_csrf_token 冒充,那是另一套。
    """
    parts = [p.strip() for p in (header or "").split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[1]
    return ""


def new_creation_id(now_ms: Optional[int] = None) -> str:
    """浏览器 creation_id = 8 位小写字母数字 + 13 位毫秒。"""
    ms = int(now_ms if now_ms is not None else time.time() * 1000)
    prefix = uuid.uuid4().hex[:8]
    return f"{prefix}{ms}"


def build_create_v2(video_id: str, title: str, desc: str, *,
                    visibility: str = "public", allow_save: bool = True,
                    poster: str = "", creation_id: str = "") -> dict[str, Any]:
    """按 2026-08-17 实发 create_v2 组 JSON。不带 blob/封面工具那种 UI 脏字段。

    探测条(214318)没 poster 也 200;公开条(221739)带了 ImageX poster。
    有 poster 就写,没有就省略。
    """
    title = (title or "").strip()[:30]
    caption = (desc or "").strip()[:2000]
    text = f"{title} {caption}".strip() if caption and caption != title else title
    vis = VISIBILITY.get(visibility, 0)
    cover: dict[str, Any] = {
        "cover_text_uri": None,
        "cover_text": None,
        "poster_delay": 0,
        "cover_tools_info": "{}",
    }
    if poster:
        cover["poster"] = poster
    now = int(time.time())
    chapter = {
        "chapter_abstract": "",
        "chapter_details": [],
        "chapter_type": 0,
        "chapter_tools_info": {
            "chapter_recommend_detail": [],
            "chapter_recommend_abstract": "",
            "chapter_source": 2,
            "chapter_recommend_type": -2,
            "create_date": now,
            "is_pc": "1",
            "is_pre_generated": "0",
            "is_syn": "1",
        },
    }
    return {
        "item": {
            "common": {
                "text": text,
                "caption": caption,
                "item_title": title,
                "activity": "[]",
                "text_extra": "[]",
                "challenges": "[]",
                "mentions": "[]",
                "hashtag_source": "",
                "hot_sentence": "",
                "interaction_stickers": "[]",
                "visibility_type": vis,
                "download": 1 if allow_save else 0,
                "timing": 0,
                "creation_id": creation_id or new_creation_id(),
                "media_type": 4,
                "video_id": video_id,
                "music_source": 0,
                "music_id": None,
            },
            "cover": cover,
            "mix": {},
            "selected_member": {"is_selected_member_video": False},
            "chapter": {
                "chapter": json.dumps(chapter, ensure_ascii=False,
                                      separators=(",", ":")),
            },
            "anchor": {},
            "sync": {"should_sync": False, "sync_to_toutiao": 0},
            "open_platform": {},
            "assistant": {"is_preview": 0, "is_post_assistant": 1},
        }
    }


def _multipart(fields: list[tuple[str, Any]]) -> tuple[bytes, str]:
    """手工拼 multipart,保证 part 顺序,不依赖 curl_cffi 的 MIME 封装。"""
    bd = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
    b = bd.encode()
    out = bytearray()
    for name, val in fields:
        out += b"--" + b + b"\r\n"
        if isinstance(val, tuple):
            fn, payload, ctype = val
            out += (f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{fn}"\r\n').encode()
            out += f"Content-Type: {ctype}\r\n\r\n".encode()
            out += payload
        else:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += str(val).encode()
        out += b"\r\n"
    out += b"--" + b + b"--\r\n"
    return bytes(out), f"multipart/form-data; boundary={bd}"


def _sigv4(method: str, path: str, query: dict[str, str], sts: dict[str, str],
           body: bytes = b"", service: str = "vod") -> dict[str, str]:
    """AWS SigV4。布局照 kv `jianying_cloud._sigv4`(已实跑 VOD)。"""
    region = VOD_REGION
    akid, secret, token = sts["access_key_id"], sts["secret_access_key"], sts["session_token"]
    t = time.gmtime()
    amzdate = time.strftime("%Y%m%dT%H%M%SZ", t)
    datestamp = time.strftime("%Y%m%d", t)
    cq = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(query.items()))
    canon_headers = f"x-amz-date:{amzdate}\nx-amz-security-token:{token}\n"
    signed = "x-amz-date;x-amz-security-token"
    ph = hashlib.sha256(body).hexdigest()
    creq = f"{method}\n{path}\n{cq}\n{canon_headers}\n{signed}\n{ph}"
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = (f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n"
               + hashlib.sha256(creq.encode()).hexdigest())

    def _mac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _mac(("AWS4" + secret).encode(), datestamp)
    k = _mac(k, region)
    k = _mac(k, service)
    k = _mac(k, "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={akid}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}"),
        "x-amz-date": amzdate,
        "x-amz-security-token": token,
    }


@dataclass
class _Slot:
    vid: str
    store_uri: str
    auth: str
    upload_id: str
    host: str
    session_key: str


class DouyinAPI:
    """一个账号一个实例。cookies 来自 `cookies_from_account`。"""

    def __init__(self, cookies: Dict[str, str], ua: str = DEFAULT_UA,
                 proxy: Optional[str] = None, *,
                 storage_state: Any = None):
        self.ck = cookies
        #: 库里的 ua 可能是 HeadlessChrome,必须洗;洗完再挑 impersonate。
        self.ua = normalize_ua(ua)
        #: 写操作要的 ticket-guard 材料。就在 creator_storage_state 的
        #: origins[creator.douyin.com].localStorage 里,登录时已随手存下。
        self._tg_store = ticket_guard.store_from_state(storage_state) \
            if storage_state else {}
        self.proxy = (proxy or "").strip() or None
        self.impersonate = impersonate_for_ua(self.ua)
        self._cli: Optional[AsyncSession] = None
        #: 只收 x-ware-csrf-token 解出来的值,不用 passport_csrf_token。
        self._csrf = ""
        self._uid = ""

    async def __aenter__(self):
        self._cli = AsyncSession(
            impersonate=self.impersonate, timeout=300, proxy=self.proxy)
        return self

    async def __aexit__(self, *exc):
        if self._cli:
            await self._cli.close()
            self._cli = None

    @property
    def cli(self) -> AsyncSession:
        if self._cli is None:
            raise DouyinAPIError("请用 `async with DouyinAPI(...) as api:`")
        return self._cli

    def _creator_headers(self, *, content_type: str = "",
                         sign_path: str = "") -> dict[str, Any]:
        """创作域请求头。**必须是 XHR 形,不能是导航形。**

        curl_cffi 的 impersonate profile 自带一套「顶层导航」默认头:
        `Sec-Fetch-Dest: document` / `Mode: navigate` / `Site: none` /
        `Sec-Fetch-User: ?1` / `Upgrade-Insecure-Requests: 1`。
        不覆盖就原样出网 —— 一个带 `Origin:` 的 POST 配这套,正好是
        **跨站表单提交**的指纹,而那恰恰是 CSRF 防护要拦的东西。
        抓包里 create_v2 的 `resource` 字段是 `xhr`。
        (2026-08-18 httpbin 回显实证;`Accept-Language` 默认还是 en-US,
        与 query 里的 `browser_language=zh-CN` 自相矛盾,一并改掉。)
        值给 `None` = 让 curl_cffi 删掉那个默认头(0.16.0 实证有效)。
        """
        h: dict[str, Any] = {
            "User-Agent": self.ua,
            "Referer": CREATOR + "/creator-micro/content/upload",
            "Origin": CREATOR,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": _cookie_header(self.ck),
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-User": None,
            "Upgrade-Insecure-Requests": None,
            "Priority": "u=1, i",
        }
        if self._csrf:
            h["x-secsdk-csrf-token"] = self._csrf
        # ⚠️ **不要**从 cookie 里抄 `bd_ticket_guard_client_data` 当请求头发。
        # 2026-08-18 CDP 抓真实浏览器(Chrome/142,活登录态)23 条 creator 域
        # XHR/fetch(含 4 条 POST):`bd-ticket-guard-*` 一条都没带 —— 它们只
        # 是 Cookie。真 ticket guard 是每请求用浏览器私钥重签的,抄一个静态值
        # 出来当头,等于**多发一个浏览器不会发的头**,比少发更可疑。
        if content_type:
            h["Content-Type"] = content_type
        if sign_path:
            # 写操作的准入凭证。缺材料**必须抛**,不能退化成不带签名发出去 ——
            # 2026-08-18 实测那样会 403 并当场烧掉整个账号会话。
            h.update(ticket_guard.headers_from_store(self._tg_store, sign_path))
        return h

    def _signed_url(self, path: str, extra: Optional[dict] = None,
                    body: str = "") -> str:
        q = {"aid": CREATOR_AID, **browser_query(self.ua),
             "msToken": gen_false_ms_token()}
        if extra:
            q.update({k: v for k, v in extra.items() if v is not None})
        signed = sign_url(urllib.parse.urlencode(q), self.ua, body)
        return f"{CREATOR}{path}?{signed}"

    def _ingest_csrf(self, resp) -> None:
        ware = ""
        try:
            ware = (resp.headers or {}).get("x-ware-csrf-token") or \
                   (resp.headers or {}).get("X-Ware-Csrf-Token") or ""
        except Exception:
            ware = ""
        token = parse_ware_csrf(ware)
        if token:
            self._csrf = token

    async def _ensure_csrf(self, path: str) -> None:
        """secsdk 路径预检。必须 HEAD + `X-Secsdk-Csrf-Request: 1` 才会下发
        `x-ware-csrf-token`。2026-08-18 小雪 id=13 实证:不带这头,HEAD/GET
        全部没 token;带了之后 HEAD create_v2 回 `0,<token>`。
        GET 同一路径不带 token,别拿 GET 当预检。
        """
        self._csrf = ""
        headers = self._creator_headers()
        headers.pop("x-secsdk-csrf-token", None)
        headers["x-secsdk-csrf-request"] = "1"
        url = self._signed_url(path)
        try:
            r = await self.cli.request("HEAD", url, headers=headers)
        except Exception as exc:                                 # noqa: BLE001
            raise DouyinAPIError(f"secsdk csrf HEAD 失败:{path} {exc!r}") from exc
        self._ingest_csrf(r)
        if self._csrf:
            return
        raise DouyinAPIError(
            f"secsdk csrf 预检失败:{path} HEAD 没有 x-ware-csrf-token,"
            f"http={getattr(r, 'status_code', '?')},未发 POST")

    async def _creator_json(self, method: str, path: str, *,
                            extra: Optional[dict] = None,
                            content: bytes | None = None,
                            content_type: str = "") -> dict[str, Any]:
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            await self._ensure_csrf(path)
        sign_body = ""
        if content:
            sign_body = content.decode("utf-8", errors="ignore") \
                if isinstance(content, (bytes, bytearray)) else str(content)
        url = self._signed_url(path, extra, body=sign_body)
        write = method.upper() != "GET"
        r = await self.cli.request(
            method, url,
            headers=self._creator_headers(content_type=content_type,
                                          sign_path=path if write else ""),
            data=content)
        self._ingest_csrf(r)
        if not r.content:
            if verify_required(r.headers):
                raise DouyinAPIError(
                    f"need_verify:{path} 要求本人身份验证 —— 抖音风控要求这个"
                    "账号先过一次短信验证码(创作者中心手动发一条即可,"
                    "或走 passport/web/send_code → validate_code),之后重发")
            raise DouyinAPIError(
                f"{method} {path} HTTP {r.status_code} 空 body"
                + (" —— 多半是 csrf/形态被拒" if r.status_code == 403 else ""))
        try:
            return r.json()
        except Exception as exc:                                 # noqa: BLE001
            raise DouyinAPIError(
                f"{method} {path} 非 JSON HTTP {r.status_code}:"
                f"{(r.text or '')[:200]}") from exc

    async def _passport_form(self, path: str, form: dict[str, str]) -> dict[str, Any]:
        """passport 域的 form POST。**不带 ticket-guard 签名**(2026-08-18 抓包
        实证:send_code/validate_code 都只要 csrf)——别照抄 create_v2 那套头。"""
        h = self._creator_headers(content_type="application/x-www-form-urlencoded")
        url = f"{CREATOR}{path}?" + urllib.parse.urlencode(
            {**_verify.QUERY, "msToken": gen_false_ms_token()})
        r = await self.cli.request(
            "POST", url, headers=h,
            data=urllib.parse.urlencode(form).encode())
        self._ingest_csrf(r)
        if not r.content:
            raise DouyinAPIError(f"{path} HTTP {r.status_code} 空 body")
        d = r.json()
        if str(d.get("message") or "") not in ("success", ""):
            raise DouyinAPIError(f"{path} 失败:{d}")
        return d.get("data") or {}

    async def send_verify_code(self, encrypt_uid: str, *,
                               scene: str = "creator") -> dict[str, Any]:
        """给账号绑定手机发 6 位验证码。→ {mobile, retry_time}。

        `encrypt_uid` 从写请求的 `x-tt-verify-passport-decision` 头里取
        (`verify.decision_from_headers`),不必另调 query_decision。
        """
        return await self._passport_form(
            _verify.SEND_PATH, _verify.code_form(encrypt_uid, scene=scene))

    async def validate_verify_code(self, encrypt_uid: str, code: str, *,
                                   scene: str = "creator") -> str:
        """提交验证码 → ticket。之后**重发原来那个写请求**即可成功。

        ⚠️ code 传人读到的 6 位数字即可,内部按 hex 编码(抓包实证)。
        """
        d = await self._passport_form(
            _verify.VALIDATE_PATH,
            _verify.code_form(encrypt_uid, scene=scene, code=code))
        return str(d.get("ticket") or "")

    async def ping(self) -> dict[str, Any]:
        """登录是否还活着。给 doctor / CLI 用。"""
        d = await self._creator_json("GET", "/aweme/v1/creator/user/info/")
        if d.get("status_code") not in (0, None) and not d.get("douyin_user_verify_info"):
            raise DouyinAPIError(f"创作者未登录:{d.get('status_msg') or d}")
        info = d.get("douyin_user_verify_info") or {}
        try:
            pc = await self._creator_json("GET", "/aweme/v1/creator/pc/user/info/")
            self._uid = str(pc.get("uid") or "")
        except DouyinAPIError:
            self._uid = ""
        return {"nickname": info.get("nick_name") or "",
                "unique_id": info.get("douyin_unique_id") or "",
                "uid": self._uid}

    async def upload_auth(self) -> dict[str, str]:
        d = await self._creator_json("GET", "/web/api/media/upload/auth/v5/")
        if d.get("status_code") not in (0, None) or not d.get("auth"):
            raise DouyinAPIError(f"upload/auth/v5 失败:{d}")
        raw = d["auth"]
        auth = json.loads(raw) if isinstance(raw, str) else raw
        return {
            "access_key_id": auth["AccessKeyID"],
            "secret_access_key": auth["SecretAccessKey"],
            "session_token": auth["SessionToken"],
            "expired_time": str(auth.get("ExpiredTime") or ""),
            "ak": str(d.get("ak") or ""),
        }

    async def _apply(self, sts: dict[str, str], file_size: int) -> _Slot:
        q = {
            "Action": "ApplyUploadInner",
            "FileSize": str(file_size),
            "FileType": "video",
            "IsInner": "1",
            "SpaceName": SPACE_NAME,
            "Version": "2020-11-19",
            "app_id": IMAGEX_APP_ID,
            "s": uuid.uuid4().hex[:16],
            "user_id": self._uid or "",
        }
        hdr = _sigv4("GET", "/top/v1", q, sts)
        hdr["user-agent"] = self.ua
        url = f"https://{VOD_HOST}/top/v1?" + urllib.parse.urlencode(q)
        r = await self.cli.get(url, headers=hdr)
        try:
            j = r.json()
        except Exception as exc:                                 # noqa: BLE001
            raise DouyinAPIError(f"ApplyUploadInner 非 JSON:{r.text[:200]}") from exc
        err = (j.get("ResponseMetadata") or {}).get("Error") or {}
        if err:
            raise DouyinAPIError(
                f"ApplyUploadInner {err.get('Code')}: {err.get('Message')}")
        node = j["Result"]["InnerUploadAddress"]["UploadNodes"][0]
        store = node["StoreInfos"][0]
        return _Slot(vid=node["Vid"], store_uri=store["StoreUri"],
                     auth=store["Auth"], upload_id=store["UploadID"],
                     host=node["UploadHost"], session_key=node["SessionKey"])

    async def _tos(self, slot: _Slot, data: bytes,
                   on_step: Callable[[str], None]) -> None:
        base = f"https://{slot.host}/upload/v1/{slot.store_uri}"
        parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
        crcs: list[str] = []
        for i, part in enumerate(parts):
            crc = format(zlib.crc32(part) & 0xFFFFFFFF, "08x")
            crcs.append(crc)
            r = await self.cli.post(
                f"{base}?uploadid={slot.upload_id}&part_number={i}&phase=transfer",
                headers={"authorization": slot.auth,
                         "x-upload-content-crc32": crc,
                         "content-type": "application/octet-stream",
                         "user-agent": self.ua},
                data=part)
            try:
                j = r.json()
            except Exception as exc:                             # noqa: BLE001
                raise DouyinAPIError(f"TOS 分片 {i} 非 JSON:{r.text[:160]}") from exc
            if j.get("code") != 2000:
                raise DouyinAPIError(f"TOS 分片 {i} 失败:{j}")
            on_step(f"分片 {i + 1}/{len(parts)}")
        finish_body = ",".join(f"{i}:{c}" for i, c in enumerate(crcs)).encode()
        r = await self.cli.post(
            f"{base}?uploadmode=part&phase=finish&uploadid={slot.upload_id}",
            headers={"authorization": slot.auth,
                     "content-type": "application/octet-stream",
                     "user-agent": self.ua},
            data=finish_body)
        try:
            j = r.json()
        except Exception as exc:                                 # noqa: BLE001
            raise DouyinAPIError(f"TOS finish 非 JSON:{r.text[:160]}") from exc
        if j.get("code") != 2000:
            raise DouyinAPIError(f"TOS finish 失败:{j}")

    async def _commit(self, sts: dict[str, str], session_key: str
                      ) -> tuple[str, dict[str, Any]]:
        q = {
            "Action": "CommitUploadInner",
            "SpaceName": SPACE_NAME,
            "Version": "2020-11-19",
            "app_id": IMAGEX_APP_ID,
            "user_id": self._uid or "",
        }
        body = json.dumps({
            "Functions": [{"Input": {"SnapshotTime": 0.0}, "Name": "Snapshot"}],
            "SessionKey": session_key,
        }, separators=(",", ":")).encode()
        hdr = _sigv4("POST", "/top/v1", q, sts, body)
        hdr["content-type"] = "application/json"
        hdr["user-agent"] = self.ua
        url = f"https://{VOD_HOST}/top/v1?" + urllib.parse.urlencode(q)
        r = await self.cli.post(url, headers=hdr, data=body)
        try:
            j = r.json()
        except Exception as exc:                                 # noqa: BLE001
            raise DouyinAPIError(f"CommitUploadInner 非 JSON:{r.text[:200]}") from exc
        err = (j.get("ResponseMetadata") or {}).get("Error") or {}
        if err:
            raise DouyinAPIError(
                f"CommitUploadInner {err.get('Code')}: {err.get('Message')}")
        res = j["Result"]["Results"][0]
        return res["Vid"], res.get("VideoMeta") or {}

    async def upload_video(self, video: str, *,
                           on_step: Callable[[str], None] = lambda s: None
                           ) -> dict[str, Any]:
        data = Path(video).read_bytes()
        on_step("upload/auth/v5")
        sts = await self.upload_auth()
        on_step("ApplyUploadInner")
        slot = await self._apply(sts, len(data))
        await self._tos(slot, data, on_step)
        on_step("CommitUploadInner")
        vid, meta = await self._commit(sts, slot.session_key)
        poster = (meta.get("PosterUri") or meta.get("CoverUri")
                  or meta.get("poster_uri") or "")
        on_step(f"上传完成 vid={vid[:16]}…")
        return {"vid": vid, "poster": poster, "meta": meta,
                "store_uri": slot.store_uri}

    async def enable_video(self, video_id: str) -> dict[str, Any]:
        """转码就绪探测。浏览器点发布前会打,status_code=0 才往下。"""
        return await self._creator_json(
            "GET", "/web/api/media/video/enable/",
            extra={"video_id": video_id})

    async def create(self, up: dict[str, Any], title: str, desc: str, *,
                     visibility: str = "public", allow_save: bool = True
                     ) -> dict[str, Any]:
        """POST create_v2 JSON。`up` 是 `upload_video` 的返回值。"""
        payload = build_create_v2(
            up["vid"], title, desc, visibility=visibility,
            allow_save=allow_save, poster=str(up.get("poster") or ""))
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        d = await self._creator_json(
            "POST", "/web/api/media/aweme/create_v2/",
            extra={"read_aid": IMAGEX_APP_ID},
            content=raw.encode(), content_type="application/json")
        if d.get("status_code") not in (0, None):
            raise DouyinAPIError(
                f"create_v2 失败 status={d.get('status_code')} "
                f"{d.get('status_msg') or d}")
        return d

    async def publish_video(self, video: str, title: str, desc: str, *,
                            visibility: str = "public", allow_save: bool = True,
                            on_step: Callable[[str], None] = lambda s: None
                            ) -> dict[str, Any]:
        await self.ping()
        up = await self.upload_video(video, on_step=on_step)
        on_step("video/enable")
        last: dict[str, Any] = {}
        for _ in range(15):
            last = await self.enable_video(up["vid"])
            if last.get("status_code") in (0, None):
                break
            await asyncio.sleep(2)
        else:
            raise DouyinAPIError(f"video/enable 失败:{last}")
        res = await self.create(up, title, desc, visibility=visibility,
                                allow_save=allow_save)
        on_step(f"create_v2 status={res.get('status_code')} "
                f"item={res.get('item_id') or ''}")
        return {"upload": up, "create": res}


async def publish_via_http(storage_state_json: str, video: str, title: str,
                           desc: str, *, topics: str = "",
                           visibility: str = "public", allow_save: bool = True,
                           ua: str = "", proxy: str = "",
                           on_step: Callable[[str], None] = lambda s: None
                           ) -> Tuple[bool, str, str]:
    """给 engine / publish_douyin 用。(ok, url, err)。"""
    ck = cookies_from_state(storage_state_json)
    if "sessionid" not in ck:
        return False, "", "logged_out:storage_state 没有 sessionid,先创作者登录"
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]
    body = ((desc or "") + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()
    try:
        async with DouyinAPI(ck, ua or DEFAULT_UA, proxy,
                             storage_state=storage_state_json) as api:
            res = await api.publish_video(
                video, title, body, visibility=visibility,
                allow_save=allow_save, on_step=on_step)
    except DouyinAPIError as exc:
        msg = str(exc)
        if "未登录" in msg or "logged" in msg.lower():
            return False, "", "logged_out:" + msg
        return False, "", msg
    create = res.get("create") or {}
    aweme = ((create.get("aweme") or create.get("item") or {})
             if isinstance(create, dict) else {})
    aweme_id = str(
        create.get("item_id")
        or aweme.get("aweme_id")
        or create.get("aweme_id")
        or "")
    url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else CREATOR
    return True, url, ""


__all__ = [
    "AID", "CREATOR", "CREATOR_AID", "DEFAULT_UA", "DouyinAPI",
    "DouyinAPIError", "IMAGEX_APP_ID", "SPACE_NAME", "VISIBILITY",
    "browser_query", "build_create_v2", "cookies_from_account",
    "cookies_from_state", "new_creation_id", "normalize_ua",
    "verify_required",
    "parse_ware_csrf",
    "publish_via_http",
]
