"""快手创作平台**纯协议**发布 —— 零浏览器。

登录态从 CreatorHub 的 sqlite(`data/creatorhub.db` 的 `storage_state`)读,
上传、签名、发布全部走 HTTP。**不 import CreatorHub 的任何代码**,
所以本仓库可以独立存在、独立提交。

════════ 协议全貌(2026-08-16 抓包实证)════════

    upload/pre            → token, fileId
    upload/frameUpload    → editSessionId          ← 必需前置,少了 finish 恒 500002
    upload/fragment × N   → 分片(必须带 Content-Range)
    upload/complete
    upload/finish         → fileId, coverKey, mediaId, photoIdStr, w/h, duration
    video/pc/submit       → 发布
    publish/refresh       → 状态
    video/pc/delete       → 删除(photoId 是 3x 开头那个短 id)

════════ 签名 ════════

`/rest/cp` 系列要签名,分两套(判据在 ks-cp/js/7708.*.js 的 `f` 名单里):

    sig4 `__NS_hxfalcon`  只有 3 个接口:fanstop/money/account/type、
                          works/v2/video/pc/edit/info、**works/v2/video/pc/submit**
    sig3 `__NS_sig3`      其余全部(含 upload/finish)

两套都是 JSVMP(自研字节码 + JS 解释器),但**不用逆算法** —— 它的 realm
对浏览器全局用 typeof 兜底,抠出来补个最小 env 就能在 Node 里跑,见 `_sign/`。

⚠️ 服务端**选择性校验**:`upload/pre`、`frameUpload`、`current/user` 不带签名
也过,只有 `upload/finish` 会拦。别拿一个接口的结果推广到全部 ——
我据此误判过「快手接口零签名」。

⚠️ 签名算的是 `md5(sortedQuery + body字符串)`,所以**签的和发的必须是同一串
字节**。这里一律先把 body 序列化成最终字符串,再拿它去签、拿它去发;
不要传 dict 给签名器让它自己 stringify(Python 与 JS 的 JSON 序列化
在空格/Unicode 转义上可能差一个字节,差一个 md5 就废)。
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

CP = "https://cp.kuaishou.com"
ZT = "https://upload.kuaishouzt.com"
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
#: 分片大小。抓包实证浏览器就是 4MB 一片。
CHUNK = 4 * 1024 * 1024
#: 抽帧:0ms 起每 1000ms 一帧,每批 2 帧 —— 与浏览器一致
FRAME_GAP_MS = 1000
FRAMES_PER_BATCH = 2
MAX_FRAMES = 8
SIGN_DIR = Path(__file__).resolve().parent / "_sign"


class KuaishouAPIError(RuntimeError):
    pass


# ── 签名 ──────────────────────────────────────────────────────────────

def _node() -> str:
    """Node 可执行文件。允许用 KS_NODE_BIN 覆盖(某些机器 node 不在 PATH)。"""
    import os
    import shutil
    return (os.environ.get("KS_NODE_BIN") or shutil.which("node")
            or str(Path.home() / "bin" / "node"))


def sign3(raw_body: str, query: Optional[Dict[str, Any]] = None) -> str:
    """`__NS_sig3=...`。`raw_body` 必须是**将要原样发出去的那串字节**。"""
    r = subprocess.run(
        [_node(), str(SIGN_DIR / "signraw.js"), "sig3", raw_body,
         json.dumps(query or {}, separators=(",", ":"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(SIGN_DIR), timeout=60)
    #: 用正则挖,不信 stdout 一定干净 —— SDK 的 log 混进来会让签名带上换行,
    #: 而那个错要到 httpx 拼 URL 时才炸,离现场很远。
    m = re.search(r"__NS_sig3=[A-Za-z0-9_\-]+", r.stdout or "")
    if not m:
        raise KuaishouAPIError(
            f"sig3 签名失败:{(r.stdout or '')[:120]} / {(r.stderr or '')[-200:]}")
    return m.group(0)


def sign4(pathname: str, raw_body: str) -> str:
    """`__NS_hxfalcon=...&caver=N`。只有 submit 等 3 个接口用。"""
    r = subprocess.run(
        [_node(), str(SIGN_DIR / "sign4.js"), pathname, raw_body],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(SIGN_DIR), timeout=60)
    m = re.search(r"__NS_hxfalcon=[A-Za-z0-9_\-$]+&caver=\d+", r.stdout or "")
    if not m:
        raise KuaishouAPIError(
            f"sig4 签名失败:{(r.stdout or '')[:120]} / {(r.stderr or '')[-200:]}")
    return m.group(0)


def sign_available() -> Tuple[bool, str]:
    """签名器能不能用(Node 在不在、SDK 文件全不全)。给 doctor 用。"""
    need = ["env.js", "jose.js", "sig3sdk.js", "signraw.js", "sign4.js"]
    missing = [n for n in need if not (SIGN_DIR / n).is_file()]
    if missing:
        #: jose.js / sig3sdk.js 是**从快手 CDN 抓的**,不入库(第三方代码
        #: 只用不收),所以新机器上必然缺 —— 这不是异常,是必走一次的初始化。
        return False, (f"缺 {', '.join(missing)} —— 跑 "
                       f"`python {SIGN_DIR}/fetch_sdk.py` 抓一次")
    try:
        sign3('{"probe":1}')
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc)[:160]
    return True, "ok"


# ── 工具 ──────────────────────────────────────────────────────────────

def _dumps(obj: Dict[str, Any]) -> str:
    """与 JS `JSON.stringify` 对齐:无空格、不转义非 ASCII。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _multipart(fields: List[Tuple[str, Any]]) -> Tuple[bytes, str]:
    """手工拼 multipart。

    不用 httpx 的 `files=` —— 传 list 形式时它会构造**同步**字节流,
    AsyncClient 直接抛 `Attempted to send an sync request`;
    而且自己拼才能保证 part 顺序与浏览器一致。
    """
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


def _ffprobe_duration_ms(path: str) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return int(float((r.stdout or "0").strip()) * 1000)
    except ValueError:
        return 0


def _frame(path: str, ms: int) -> bytes:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{ms/1000:.3f}",
                        "-i", path, "-frames:v", "1", "-f", "image2",
                        "-c:v", "mjpeg", "-q:v", "2", "-"], capture_output=True)
    return r.stdout


def cookies_from_account(acc) -> Dict[str, str]:
    """从 DB 的 `storage_state` 直接取快手域 cookie —— **不启动浏览器**。

    这条是首选。启动浏览器取 cookie 有两个实际代价:
      · 与正在跑的 CreatorHub 服务**抢 profile 锁**(`BrowserProfileConflictError`),
        服务开着就取不到;
      · 一次冷启动好几秒,而这条链路本来是为了「不碰浏览器」。

    同名 cookie 可能在多个 domain 下各有一份(如 `.kuaishou.com` 与
    `cp.kuaishou.com`),**取更具体的那个** —— 创作平台的登录态在 cp 子域上。
    """
    raw = getattr(acc, "storage_state", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    out: Dict[str, str] = {}
    #: 按 domain 长度升序放,越具体的越后写、覆盖掉泛域名的
    rows = [c for c in data.get("cookies", [])
            if "kuaishou" in (c.get("domain") or "")]
    for c in sorted(rows, key=lambda c: len(c.get("domain") or "")):
        out[c["name"]] = c["value"]
    return out


async def cookies_from_profile(acc, profiles_root: str,
                               default_ua: str = DEFAULT_UA) -> Dict[str, str]:
    """从账号 profile 取快手域 cookie(**回落用**,会启动 headless 浏览器)。

    ⚠️ 这条要求能 import 到 CreatorHub 的 `application.browser`(把 CreatorHub 根目录
    放进 sys.path)。本仓库**不依赖**它:正常路径是 `cookies_from_account`,
    只读 sqlite,零依赖。这里留着只是给「storage_state 空了」兜底。
    """
    from application.browser.manager import BrowserManager
    mgr = BrowserManager(default_ua, profiles_root)
    await mgr.start()
    try:
        ctx = await mgr.context_for(mgr.identity_for(acc))
        raw = await ctx.cookies()
    finally:
        await mgr.stop()
    return {c["name"]: c["value"] for c in raw
            if "kuaishou" in (c.get("domain") or "")}


# ── API ───────────────────────────────────────────────────────────────

class KuaishouAPI:
    """一个账号一个实例。`cookies` 来自 `cookies_from_profile`。"""

    def __init__(self, cookies: Dict[str, str], ua: str = DEFAULT_UA,
                 proxy: Optional[str] = None):
        self.ck = cookies
        self.ph = cookies.get("kuaishou.web.cp.api_ph", "")
        self.ua = ua
        self.proxy = proxy or None
        #: `kww` 是风控头,值就是 cookie 里的 kwfv1(一次会话恒定)
        self.h = {
            "User-Agent": ua, "Origin": CP, "Accept": "*/*",
            "Referer": CP + "/article/publish/video",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            "kww": cookies.get("kwfv1", ""),
        }
        self.jh = {**self.h, "Content-Type": "application/json;charset=UTF-8"}
        self._cli: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._cli = httpx.AsyncClient(timeout=300, headers=self.h,
                                      proxy=self.proxy)
        return self

    async def __aexit__(self, *exc):
        if self._cli:
            await self._cli.aclose()

    @property
    def cli(self) -> httpx.AsyncClient:
        if self._cli is None:
            raise KuaishouAPIError("请用 `async with KuaishouAPI(...) as api:`")
        return self._cli

    async def _post_json(self, path: str, body: Dict[str, Any], *,
                         sig: str = "sig3") -> Dict[str, Any]:
        """签名 + 发送。body 先定型成字符串,签的发的是同一串。"""
        body.setdefault("kuaishou.web.cp.api_ph", self.ph)
        raw = _dumps(body)
        q = sign4(path, raw) if sig == "sig4" else sign3(raw)
        r = await self.cli.post(f"{CP}{path}?{q}", headers=self.jh, content=raw)
        try:
            return r.json()
        except Exception:                                     # noqa: BLE001
            raise KuaishouAPIError(f"{path} 返回非 JSON:{r.text[:200]}")

    async def upload_video(self, video: str, *,
                           on_step: Callable[[str], None] = lambda s: None
                           ) -> Dict[str, Any]:
        """上传视频,返回 `upload/finish` 的 data(fileId/coverKey/mediaId/...)。"""
        data = Path(video).read_bytes()
        name = Path(video).name

        pre = await self._post_json("/rest/cp/works/v2/video/pc/upload/pre",
                                    {"uploadType": 1})
        if pre.get("result") != 1:
            raise KuaishouAPIError(f"upload/pre 失败:{pre}")
        token = pre["data"]["token"]
        on_step("upload/pre ok")

        #: frameUpload 是**必需前置**。editSessionId 首批留空,
        #: 服务端在响应里给,后续批次原样带回。
        sid = ""
        dur_ms = _ffprobe_duration_ms(video) or 1000
        offsets = [i * FRAME_GAP_MS
                   for i in range(min(MAX_FRAMES, dur_ms // FRAME_GAP_MS + 1))]
        frames = [(o, _frame(video, o)) for o in (offsets or [0])]
        frames = [(o, b) for o, b in frames if b]
        for bi in range(0, len(frames), FRAMES_PER_BATCH):
            batch = frames[bi:bi + FRAMES_PER_BATCH]
            fields: List[Tuple[str, Any]] = [
                ("kuaishou.web.cp.api_ph", self.ph), ("editSessionId", sid)]
            fields += [("frameList", (f"{bi+j}.jpeg", b, "image/jpeg"))
                       for j, (_, b) in enumerate(batch)]
            fields.append(("batchNo", str(bi // FRAMES_PER_BATCH + 1)))
            fields += [("offsetList", str(o)) for o, _ in batch]
            body, ct = _multipart(fields)
            r = await self.cli.post(
                f"{CP}/rest/cp/works/v2/video/pc/upload/frameUpload",
                content=body, headers={**self.h, "Content-Type": ct})
            j = r.json()
            if j.get("result") != 1:
                raise KuaishouAPIError(f"frameUpload 失败:{r.text[:160]}")
            sid = (j.get("data") or {}).get("editSessionId", sid)
        on_step(f"frameUpload ok（{len(frames)} 帧）")

        await self.cli.get(f"{ZT}/api/upload/resume",
                           params={"upload_token": token})
        parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
        off = 0
        for i, part in enumerate(parts):
            #: Content-Range 不能省 —— 服务端靠它拼回原文件。少了它每片
            #: 照样 result:1、complete 也 result:1,直到 finish 才以
            #: 「请稍后重试」的面目出现。
            r = await self.cli.post(
                f"{ZT}/api/upload/fragment",
                params={"upload_token": token, "fragment_id": i},
                content=part,
                headers={**self.h, "Content-Type": "application/octet-stream",
                         "Content-Range":
                             f"bytes {off}-{off+len(part)-1}/{len(data)}"})
            if r.json().get("result") != 1:
                raise KuaishouAPIError(f"分片 {i} 失败:{r.text[:160]}")
            off += len(part)
            on_step(f"分片 {i+1}/{len(parts)}")
        await self.cli.post(f"{ZT}/api/upload/complete",
                            params={"fragment_count": len(parts),
                                    "upload_token": token})

        fin = await self._post_json(
            "/rest/cp/works/v2/video/pc/upload/finish",
            {"token": token, "fileName": name, "fileType": "video/mp4",
             "fileLength": len(data)})
        if fin.get("result") != 1:
            raise KuaishouAPIError(f"upload/finish 失败:{fin}")
        on_step(f"upload/finish ok  fileId={fin['data'].get('fileId')}")
        return fin["data"]

    async def submit(self, up: Dict[str, Any], caption: str, *,
                     duration_ms: int = 0,
                     allow_same_frame: bool = True,
                     download_type: int = 1) -> Dict[str, Any]:
        """发布。`up` 是 `upload_video` 的返回值。

        字段表照抓包实证复刻,只有 caption / fileId / coverKey / mediaId /
        photoIdStr / videoDuration 是变的,其余是网页版的默认值。
        """
        body = {
            "fileId": up.get("fileId"), "coverKey": up.get("coverKey"),
            "coverTimeStamp": 0, "caption": caption,
            "photoStatus": 1, "coverType": 1, "coverTitle": "", "photoType": 0,
            "collectionId": "", "publishTime": 0, "longitude": "", "latitude": "",
            "poiId": 0, "notifyResult": 0, "domain": "", "secondDomain": "",
            "coverCropped": False, "pkCoverKey": "", "profileCoverKey": "",
            "downloadType": download_type, "disableNearbyShow": False,
            "allowSameFrame": allow_same_frame, "movieId": "",
            "openPrePreview": False, "declareInfo": {}, "activityIds": [],
            "riseQuality": False, "chapters": [], "useAiCaptionCover": False,
            "useAiCaption": False, "isUseIdealTime": False, "useAiCover": False,
            "kceInfo": "", "coverSize": "", "pkCoverTimeStamp": -1,
            "pkCoverType": 1, "pkCoverSize": "", "innerChannel": 0,
            "mediaId": up.get("mediaId", ""), "videoInfoMeta": "",
            "triggerH265": False, "recTagIdList": [], "onvideoDuration": 0,
            "disallowRecreation": False, "previewUrlErrorMessage": "",
            "coPublishUser": [], "coPublishRole": 0, "extraInfo": "",
            "videoDuration": duration_ms or int((up.get("duration") or 0) * 1000),
            "projectId": "", "photoIdStr": up.get("photoIdStr", ""),
            "activity": [],
        }
        #: ⚠️ submit 走 **sig4**,不是 sig3(名单见模块 docstring)
        return await self._post_json("/rest/cp/works/v2/video/pc/submit",
                                     body, sig="sig4")

    async def delete(self, photo_id: str) -> Dict[str, Any]:
        """删除一条作品。`photo_id` 是 `3x` 开头那个短 id,不是 photoIdStr。

        (2026-08-16 抓包:走 sig3,body 只要 photoId + api_ph。)
        """
        return await self._post_json("/rest/cp/works/v2/video/pc/delete",
                                     {"photoId": photo_id})

    async def publish_status(self, ids: List[int]) -> Dict[str, Any]:
        return await self._post_json(
            "/rest/cp/works/v2/video/pc/publish/refresh", {"ids": ids})

    async def publish_video(self, video: str, caption: str, *,
                            on_step: Callable[[str], None] = lambda s: None
                            ) -> Dict[str, Any]:
        """端到端:上传 → 发布。返回 `{"upload":..., "submit":...}`。"""
        up = await self.upload_video(video, on_step=on_step)
        dur = _ffprobe_duration_ms(video)
        res = await self.submit(up, caption, duration_ms=dur)
        on_step(f"submit result={res.get('result')} {res.get('message')}")
        if res.get("result") != 1:
            raise KuaishouAPIError(f"发布失败:{res}")
        return {"upload": up, "submit": res}


__all__ = ["CP", "ZT", "DEFAULT_UA", "KuaishouAPI", "KuaishouAPIError",
           "cookies_from_account", "cookies_from_profile",
           "sign3", "sign4", "sign_available"]
