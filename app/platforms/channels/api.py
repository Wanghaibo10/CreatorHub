"""视频号发布 —— 纯 HTTP 接口版(不开浏览器、不点按钮)。

为什么有这个文件:UI 版 `publish.py` 要跟 wujie 微前端、shadowRoot、隐藏模板、
浮层顺序死磕(操作顺序写反就 3/3 静默失败,一个报错都没有)。而真号抓包证明整条
发布链路**没有 JS 签名**,起点就是登录态 cookie,所以完全可以纯 HTTP 走完。

链路(2026-08-01 真号抓包实证,细节见 docs/channels-api-reverse.md):

    helper_upload_params                       -> authKey(上传三接口的 authorization)
    applyuploaddfs/uploadpartdfs/completepart  -> DownloadURL(视频、封面各走一遍)
    post_clip_video                            -> clipKey(= draftId = videoClipTaskId)
    post_clip_video_result 轮询                 -> flag=2 转码完成
    post_create                                -> 发布

UI 版留着做兜底 —— 平台改版时接口和 UI 通常不会同时挂。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("creatorhub.channels.api")

HOST = "https://channels.weixin.qq.com"
BIZ = f"{HOST}/cgi-bin/mmfinderassistant-bin"                    # 主 frame 的接口
MICRO = f"{HOST}/micro/content/cgi-bin/mmfinderassistant-bin"    # 发布页(微前端)的接口
UPLOAD_HOST = "https://finderassistancea.video.qq.com"           # 域名 a/c/d/e 等价,固定用 a

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")

CHUNK = 8 * 1024 * 1024      # 抓包实测前端按 8MB 切片

# 「含AI生成内容」标注。tagType 取自 post/finder_get_object_tag_list(0=无需标注,
# 1=含AI生成内容,2=营销广告,3=虚构剧情,8=个人观点)。
AI_TAG_TYPE = 1
# tagKey 是**前端自己造的**,那个接口并不下发它。实测两次会话的结构:
#   fb3cc7cf9dc5da91 1785574494262 36AE2F5C565C0DA5
#   5588cfe246d4d4e5 1785574225089 36AE2F5C565C0DA5
#   └─16位随机 hex─┘ └─13位毫秒戳─┘ └──固定尾巴,两次完全相同──┘
_TAG_KEY_SUFFIX = "36AE2F5C565C0DA5"


class ChannelsError(RuntimeError):
    """接口返回 errCode != 0,或链路中途拿不到必需字段。"""


# ────────────────────────── 纯函数工具 ──────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


def _rid() -> str:
    return f"{os.urandom(4).hex()}-{os.urandom(4).hex()}"


def _ms() -> str:
    return str(int(time.time() * 1000))


def gen_tag_key() -> str:
    return f"{os.urandom(8).hex()}{_ms()}{_TAG_KEY_SUFFIX}"


def cdn_url(download_url: str) -> str:
    """completepartuploaddfs 返回 http://wxapp.tc.qq.com/...,而发布接口要的是
    https://finder.video.qq.com/... —— 只换 scheme+host,query 原样保留。
    (抓包比对:post_clip_video 的 url 与 post_create 的 media.url 逐字节相同,
     两者都是这么换出来的。)"""
    return re.sub(r"^https?://[^/]+", "https://finder.video.qq.com", download_url)


def block_parts(size: int, chunk: int = CHUNK) -> List[int]:
    """算 applyuploaddfs 的 BlockPartLength。

    ⚠️ 它是每片的**累计结束偏移**,不是片长 —— 这点很容易看错。实测:
        19996989B -> [8388608, 16777216, 19996989]   (8MB/8MB/3.2MB 三片)
          136679B -> [136679]                        (单片)
    """
    out: List[int] = []
    off = 0
    while off < size:
        off = min(off + chunk, size)
        out.append(off)
    return out or [0]


def build_topic_xml(description: str) -> str:
    """把带 #话题 的正文拼成 objectDesc.topic.finderTopicInfo 要的 XML。

    正文 "正文…\\n#情感 #治愈\\n" 会被切成:
        value0 = 正文…\\n          (纯文本)
        value1 = <topic>#情感#</topic>
        value2 = " "               (话题之间的空格也算一段)
        value3 = <topic>#治愈#</topic>
        value4 = "\\n"
    valuecount = 段数。格式照抄抓包,一个字都别改。
    """
    segs: List[Tuple[str, str]] = []
    pos = 0
    for m in re.finditer(r"#([^\s#]+)", description):
        if m.start() > pos:
            segs.append(("text", description[pos:m.start()]))
        segs.append(("topic", m.group(1)))
        pos = m.end()
    if pos < len(description):
        segs.append(("text", description[pos:]))

    body = []
    for i, (kind, val) in enumerate(segs):
        inner = (f"<topic><![CDATA[#{val}#]]></topic>" if kind == "topic"
                 else f"<![CDATA[{val}]]>")
        body.append(f"<value{i}>{inner}</value{i}>")
    return (f"<finder><version>1</version><valuecount>{len(segs)}</valuecount>"
            f"<style><at></at></style>{''.join(body)}</finder>")


def extract_topics(description: str) -> List[str]:
    """post_create 的顶层 topics 字段 = 话题名数组(不带 #),顺序同正文。"""
    seen, out = set(), []
    for t in re.findall(r"#([^\s#]+)", description):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def probe_video(path: str | Path) -> Dict[str, Any]:
    """ffprobe 取宽高/时长/字节数。发布接口这几个值必须和真实文件对得上。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    st = d["streams"][0]
    return {"width": int(st["width"]), "height": int(st["height"]),
            "duration": float(d["format"]["duration"]),
            "fileSize": Path(path).stat().st_size}


def extract_cover(video: str | Path, out: str | Path, at: float = 0.5) -> Path:
    """抽一帧当封面。视频号不会替你生成 —— post_create 的 thumbUrl/coverUrl
    必填,而且指向 20304(图片)类型的独立上传结果。"""
    out = Path(out)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(at),
                    "-i", str(video), "-vframes", "1", "-q:v", "2", str(out)],
                   check=True)
    return out


def cookie_header(cookies: Dict[str, str] | List[dict]) -> str:
    """接受 {name: value} 或 playwright 的 cookie 列表,只取发布必需的两项。
    实测 post_create 只认 sessionid + wxuin,别的都可以不带。"""
    if isinstance(cookies, list):
        cookies = {c["name"]: c["value"] for c in cookies if c.get("name")}
    need = [k for k in ("sessionid", "wxuin") if k in cookies]
    if "sessionid" not in cookies:
        raise ChannelsError("cookie 里没有 sessionid —— 登录态没取到,先在 CreatorHub 里扫码")
    return "; ".join(f"{k}={cookies[k]}" for k in need)


# ────────────────────────── 客户端 ──────────────────────────

class ChannelsAPI:
    """一个账号一个实例。全程只用 cookie,无签名、无浏览器。"""

    def __init__(self, cookie: str, finder_id: str, uin: str,
                 ua: str = DEFAULT_UA, proxy: Optional[str] = None,
                 timeout: float = 180.0):
        self.cookie = cookie
        self.finder_id = finder_id
        self.uin = str(uin or "")
        self.ua = ua
        self._cli = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0),
            proxy=proxy or None, follow_redirects=True,
            headers={"user-agent": ua, "cookie": cookie})

    async def aclose(self):
        await self._cli.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # ── 公共 body / 请求 ──

    def _meta(self) -> Dict[str, Any]:
        """每个 mmfinderassistant-bin 接口都带这一坨。缺了会被判非法请求。"""
        return {"timestamp": _ms(), "_log_finder_uin": None,
                "_log_finder_id": self.finder_id, "rawKeyBuff": "",
                "pluginSessionId": None, "scene": 7, "reqScene": 7}

    async def call(self, path: str, body: Optional[Dict[str, Any]] = None,
                   micro: bool = True) -> Dict[str, Any]:
        base = MICRO if micro else BIZ
        # 主 frame 与微前端的 referer/x-wechat-uin 不一样,照抓包区分(实测混用也能通,
        # 但没必要给风控留特征)。
        ref = (f"{HOST}/micro/content/post/create" if micro
               else f"{HOST}/platform/post/create")
        url = f"{base}/{path.lstrip('/')}?_aid={_uuid()}&_rid={_rid()}"
        payload = dict(body or {})
        payload.update(self._meta())
        r = await self._cli.post(url, json=payload, headers={
            "content-type": "application/json", "origin": HOST, "referer": ref,
            "x-wechat-uin": self.uin if micro else "0000000000"})
        r.raise_for_status()
        d = r.json()
        if d.get("errCode") != 0:
            raise ChannelsError(f"{path} errCode={d.get('errCode')} {d.get('errMsg')}")
        return d.get("data") or {}

    # ── 上传 ──

    async def upload_params(self) -> Dict[str, Any]:
        """上传鉴权与文件类型码的唯一来源,只靠 cookie。

        返回 authKey(原样就是上传三接口的 authorization 头)、uin、appType、scene、
        以及 videoFileType=20302 / pictureFileType=20304 等类型码。
        ⚠️ 这里的 uin(4205297995)是**视频号号主 uin**,和 cookie 里的 wxuin 不是一个数,
        x-arguments 的 weixinnum 要用这个。
        """
        return await self.call("helper/helper_upload_params", micro=False)

    async def upload_file(self, data: bytes, params: Dict[str, Any], *,
                          file_type: int, file_key: str) -> str:
        """分片上传,返回可直接用于发布的 finder.video.qq.com 地址。

        ⚠️ `x-arguments` 头是**必需**的,少了这个 COS 网关直接 404(空 body、无错误信息,
        很难猜)。它不是签名,就是一串明文参数,但服务端靠它认这次上传是什么文件。
        """
        auth_key = params["authKey"]
        # 同一次上传(applyuploaddfs/uploadpartdfs/complete 三步)共用一个 taskid
        args = (f"apptype={params.get('appType', 251)}&filetype={file_type}"
                f"&weixinnum={params.get('uin', self.uin)}&filekey={file_key}"
                f"&filesize={len(data)}&taskid={_uuid()}"
                f"&scene={params.get('scene', 2)}")
        hdr = {"authorization": auth_key, "origin": HOST, "referer": f"{HOST}/",
               "x-arguments": args, "accept": "application/json, text/plain, */*",
               "accept-language": "zh-CN"}
        parts = block_parts(len(data))

        r = await self._cli.request(
            "PUT", f"{UPLOAD_HOST}/applyuploaddfs",
            json={"BlockSum": len(parts), "BlockPartLength": parts},
            headers={**hdr, "content-type": "application/json", "content-md5": "null"})
        r.raise_for_status()
        upload_id = (r.json() or {}).get("UploadID")
        if not upload_id:
            raise ChannelsError(f"applyuploaddfs 没给 UploadID: {r.text[:200]!r}")

        info, start = [], 0
        for i, end in enumerate(parts, 1):
            chunk = data[start:end]
            start = end
            # content-md5 是**该分片**的 md5(小写 hex,不带引号);响应回的 ETag 就是它加引号
            md5 = hashlib.md5(chunk).hexdigest()
            etag = None
            for attempt in range(3):
                try:
                    pr = await self._cli.request(
                        "PUT", f"{UPLOAD_HOST}/uploadpartdfs",
                        params={"PartNumber": i, "UploadID": upload_id}, content=chunk,
                        headers={**hdr, "content-type": "application/octet-stream",
                                 "content-md5": md5})
                    pr.raise_for_status()
                    etag = (pr.json() or {}).get("ETag")
                    if etag:
                        break
                    log.warning("分片 %d 无 ETag: %s", i, pr.text[:200])
                except Exception as e:                      # noqa: BLE001
                    log.warning("分片 %d 第 %d 次失败: %s", i, attempt + 1, e)
                    await asyncio.sleep(2)
            if not etag:
                raise ChannelsError(f"分片 {i}/{len(parts)} 上传失败")
            info.append({"PartNumber": i, "ETag": etag})
            log.info("分片 %d/%d ok (%d B)", i, len(parts), len(chunk))

        cr = await self._cli.post(
            f"{UPLOAD_HOST}/completepartuploaddfs", params={"UploadID": upload_id},
            json={"TransFlag": "0_0", "PartInfo": info},
            headers={**hdr, "content-type": "application/json", "content-md5": "null"})
        cr.raise_for_status()
        dl = (cr.json() or {}).get("DownloadURL")
        if not dl:
            raise ChannelsError(f"completepartuploaddfs 没给 DownloadURL: {cr.text[:200]!r}")
        return cdn_url(dl)

    # ── 转码 ──

    async def trace_key(self) -> str:
        return (await self.call("post/get-finder-post-trace-key",
                                {"objectId": ""})).get("traceKey", "")

    async def clip_video(self, url: str, meta: Dict[str, Any], trace: Dict[str, Any]
                         ) -> str:
        """把上传好的视频提交给视频号,换 clipKey(就是发布要的 videoClipTaskId)。"""
        d = await self.call("post/post_clip_video", {
            "url": url, "timeStart": 0, "cropDuration": 0,
            "height": meta["height"], "width": meta["width"], "x": 0, "y": 0,
            "clipOriginVideoInfo": {"width": meta["width"], "height": meta["height"],
                                    "duration": meta["duration"],
                                    "fileSize": meta["fileSize"]},
            "traceInfo": trace,
            "targetWidth": meta["width"], "targetHeight": meta["height"],
            "type": 4, "useAstraThumbCover": 1})
        key = d.get("clipKey")
        if not key:
            raise ChannelsError(f"post_clip_video 没给 clipKey: {d}")
        return key

    async def wait_clip(self, clip_key: str, timeout: float = 300.0,
                        interval: float = 5.0) -> Dict[str, Any]:
        """轮询到 flag=2(转码完成)。19MB 视频实测 10 秒内就好。"""
        deadline = time.time() + timeout
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = await self.call("post/post_clip_video_result",
                                   {"clipKey": clip_key, "draftId": clip_key})
            if last.get("flag") == 2:
                return last
            await asyncio.sleep(interval)
        raise ChannelsError(f"转码超时({timeout}s),最后一次 flag={last.get('flag')}")

    # ── 发布 ──

    async def post_create(self, *, clip_key: str, video_url: str, cover_url: str,
                          description: str, meta: Dict[str, Any],
                          trace: Dict[str, Any], upload_cost: int,
                          short_title: str = "", original: bool = True,
                          ai_generated: bool = True) -> Dict[str, Any]:
        """真正的发布请求。body 骨架逐字段照抄抓包(4225B 那条)。"""
        body: Dict[str, Any] = {
            "objectType": 0,
            "longitude": 0, "latitude": 0, "feedLongitude": 0, "feedLatitude": 0,
            # ⚠️ originalFlag 是**干扰项**:勾没勾原创它都是 0。原创声明看 postFlag。
            # 这条只有做「勾/不勾」对照实验才看得出来,照抄单次抓包会静默发出非原创。
            "originalFlag": 0,
            "topics": extract_topics(description),
            "isFullPost": 1, "handleFlag": 2,
            "videoClipTaskId": clip_key,
            "traceInfo": trace,
            "objectDesc": {
                "mpTitle": "",
                "description": description,
                "extReading": {},
                "mediaType": 4,
                "location": {},          # 空 = 不显示位置
                "topic": {"finderTopicInfo": build_topic_xml(description)},
                "event": {},
                "mentionedUser": [],
                "media": [{
                    "url": video_url,
                    "fileSize": str(meta["fileSize"]),
                    # 四个封面字段填同一个地址(抓包实测完全相同)
                    "thumbUrl": cover_url, "fullThumbUrl": cover_url,
                    "coverUrl": cover_url, "fullCoverUrl": cover_url,
                    "mediaType": 4,
                    "videoPlayLen": int(meta["duration"]),
                    "width": meta["width"], "height": meta["height"],
                    # 名字叫 md5sum,抓到的值却是 uuid4(6107e52d-9186-...)。
                    # 服务端显然不校验内容哈希,随机生成即可。
                    "md5sum": _uuid(),
                    "urlCdnTaskId": clip_key,
                }],
                "shortTitle": [{"shortTitle": short_title}] if short_title else [],
                "member": {},
            },
            "report": {
                "clipKey": clip_key, "draftId": clip_key,
                "height": meta["height"], "width": meta["width"],
                "duration": meta["duration"], "fileSize": meta["fileSize"],
                "uploadCost": upload_cost, **self._meta(),
            },
            "postFlag": 1 if original else 0,      # ← 原创声明在这
            "mode": 1,
            "clientid": _uuid(),
        }
        if ai_generated:
            body["tagInfo"] = {"tagType": AI_TAG_TYPE, "tagKey": gen_tag_key()}
        return await self.call("post/post_create", body)

    # ── 一把梭 ──

    async def publish_video(self, video: str | Path, description: str, *,
                            cover: Optional[str | Path] = None,
                            short_title: str = "", original: bool = True,
                            ai_generated: bool = True,
                            on_step=None) -> Dict[str, Any]:
        """上传 → 转码 → 发布。返回 post_create 的 data。

        ⚠️ 发布是**异步**的:接口返回成功后作品可能几分钟才出现在列表里。
        列表里暂时查不到 ≠ 没发成功,**绝对不要**直接重发(踩过,线上出现两条)。
        """
        video = Path(video)
        step = on_step or (lambda s: log.info("%s", s))
        meta = probe_video(video)
        step(f"视频 {meta['width']}x{meta['height']} {meta['duration']:.1f}s "
             f"{meta['fileSize']/1048576:.1f}MB")

        tmp_cover = None
        if cover is None:
            tmp_cover = video.with_name(f".cover_{os.getpid()}.jpg")
            extract_cover(video, tmp_cover)
            cover = tmp_cover
            step(f"抽帧封面 {Path(cover).stat().st_size} B")

        try:
            params = await self.upload_params()
            if not params.get("authKey"):
                raise ChannelsError(f"没拿到 authKey(登录态可能失效): {params}")
            trace_key = await self.trace_key()

            t0 = int(time.time())
            step("上传视频…")
            video_url = await self.upload_file(
                video.read_bytes(), params,
                file_type=params.get("videoFileType", 20302), file_key=video.name)
            step("上传封面…")
            cover_url = await self.upload_file(
                Path(cover).read_bytes(), params,
                file_type=params.get("pictureFileType", 20304),
                # 文件名照抄前端,别自作主张 —— 服务端按它推断这是视频封面
                file_key="finder_video_img.jpeg")
            t1 = int(time.time())
            trace = {"traceKey": trace_key, "uploadCdnStart": t0, "uploadCdnEnd": t1}

            clip_key = await self.clip_video(video_url, meta, trace)
            step(f"clipKey={clip_key},等转码…")
            await self.wait_clip(clip_key)
            step("转码完成,提交发布…")

            data = await self.post_create(
                clip_key=clip_key, video_url=video_url, cover_url=cover_url,
                description=description, meta=meta, trace=trace,
                upload_cost=(t1 - t0) * 1000, short_title=short_title,
                original=original, ai_generated=ai_generated)
            step(f"已提交:原创={original} AI标注={ai_generated}")
            return data
        finally:
            if tmp_cover and tmp_cover.exists():
                tmp_cover.unlink(missing_ok=True)

    # ── 查列表(发布后核对用) ──

    async def recent_posts(self, limit: int = 10) -> List[dict]:
        """线上作品列表。注意每项的 `desc` 是 **dict** 不是字符串,正文在
        `it["desc"]["description"]`。

        原创是否生效看 `it["originalInfo"]["isDeclared"]`(1=已声明)。
        ⚠️ 列表接口**不返回** tagInfo,「含AI生成内容」这一项没法从这里核对,
        只能去作品页看有没有「作者提示: 含AI生成内容」。
        """
        d = await self.call("post/post_list", {"pageSize": limit, "currentPage": 1,
                                               "onlyUnread": False})
        return d.get("list") or d.get("post_list") or d.get("object_list") or []


# ────────────────────────── 登录态获取 ──────────────────────────

async def cookies_from_profile(acc, profiles_root: str, default_ua: str
                               ) -> Tuple[str, str]:
    """从账号的 Playwright profile 里取 cookie,返回 (cookie_header, uin)。

    这里仍会起一次 **headless** 浏览器,但只为读 cookie,不点任何东西 —— 因为
    视频号的 sessionid 是 session cookie,落盘的 profile 里不一定有,得让
    BrowserManager 走一遍它的登录态桥接逻辑(DB storage_state -> context)。
    """
    from ...browser.manager import BrowserManager
    mgr = BrowserManager(default_ua, profiles_root)
    await mgr.start()
    try:
        ctx = await mgr.context_for(mgr.identity_for(acc))
        raw = await ctx.cookies()
    finally:
        await mgr.stop()
    jar = {c["name"]: c["value"] for c in raw
           if "weixin.qq.com" in (c.get("domain") or "")}
    return cookie_header(jar), jar.get("wxuin", "")


async def resolve_finder_id(cookie: str, uin: str, ua: str = DEFAULT_UA,
                            proxy: Optional[str] = None) -> str:
    """finder_id(v2_0600...@finder)是每个请求的 _log_finder_id。
    从 auth_data 拿,免得写死在配置里。"""
    async with httpx.AsyncClient(timeout=30, proxy=proxy or None,
                                 headers={"user-agent": ua, "cookie": cookie}) as c:
        r = await c.post(f"{BIZ}/auth/auth_data?_aid={_uuid()}&_rid={_rid()}",
                         json={"timestamp": _ms(), "_log_finder_uin": None,
                               "_log_finder_id": "", "rawKeyBuff": "",
                               "pluginSessionId": None, "scene": 7, "reqScene": 7},
                         headers={"content-type": "application/json", "origin": HOST,
                                  "referer": f"{HOST}/platform/post/create",
                                  "x-wechat-uin": str(uin or "0000000000")})
        r.raise_for_status()
        d = r.json()
    blob = json.dumps(d, ensure_ascii=False)
    m = re.search(r"(v2_[0-9a-f]+@finder)", blob)
    if not m:
        raise ChannelsError(f"auth_data 里没找到 finder_id: {blob[:400]}")
    return m.group(1)
