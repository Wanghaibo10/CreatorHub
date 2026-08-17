"""百家号图文发布协议。纯 HTTP,完全脱离浏览器。

协议移植自 quote-video/scripts/baijiahao_publish.py(2026-08-08/10 实测线)。

## 三个凭证各自从哪来

| 凭证 | 来源 | 有效期 |
|------|------|--------|
| cookie | 账号库里的 Cookie 串(登录一次导出) | BDUSS 按月 |
| token(JWT) | **GET 编辑页,从 HTML 里正则抠** `__BJH__INIT__AUTH__` | 约 12h |
| Acs-Token | **纯 Node 生成** `_acs/gen_acs.js` | 一次性(第二段是生成时刻毫秒戳) |

⚠️ 两条被实测推翻的旧结论,别走回头路:

1. ~~"Acs-Token 必须浏览器现算,补环境是周级工作量"~~ —— 那是从静态特征推测的。
   实跑:acs-2112.js 在 Node vm 沙箱里只摸 12 个全局属性,不碰 canvas,不要 jsdom;
   唯一的坎是它拿 DOM **构造函数**做校验(`Window is not defined`),补上就出 token。
2. ~~"发布接口必须带 Acs-Token"~~ —— 零副作用探针(故意提交空标题看拦在哪层)跑了
   4 组、间隔 75s 排除限流:带真 token / 带瞎编的 / 完全不带,**三种都直达
   `errno=20040030 标题不能为空`**,服务端根本没校验它。照常带上(成本已是零)。

仍然为真:
  · 正文是 **HTML 不是 markdown**(接口层)
  · `BJH_FE_NOUNCE` 服务端不校验,随机 32 hex 即可
  · 图片上传 `/pcui/picture/processproxy` 只要 token,base64 值**以逗号开头**
  · `<img>` 必须带 `data-bjh-origin-src` 和 `data-bjh-params`,否则编辑器里图片不渲染
  · 封面平台必填,只支持 1 张(one)或 3 张(three)两种版式
  · **`save` 是草稿、`publish` 是发布**(与今日头条相反)
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import struct
import asyncio
import subprocess
import urllib.parse
from pathlib import Path

from application.base import (ArticleDraft, ArticlePlatformSession, ArticleResult, AuthExpired, PlatformError, resolve_local)

BJH = "https://baijiahao.baidu.com"
EDIT_URL = f"{BJH}/builder/rc/edit?type=news"
ACS_SCRIPT = Path(__file__).parent / "_acs" / "gen_acs.js"
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def image_size(p: Path) -> tuple[int, int]:
    """读 PNG/JPEG 宽高(<img data-w/data-h> 要用,不强依赖 PIL)。"""
    b = p.read_bytes()
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", b[16:24])
        return w, h
    if b[:2] == b"\xff\xd8":                      # JPEG:扫 SOF 段
        i = 2
        while i < len(b) - 9:
            if b[i] != 0xFF:
                i += 1
                continue
            m = b[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return w, h
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.size
    except Exception:
        raise PlatformError(f"读不出图片尺寸(只内置支持 PNG/JPEG):{p}")


def node_acs() -> str:
    """纯 Node 生成 Acs-Token(不开浏览器)。一次性,取完立刻用。"""
    if not ACS_SCRIPT.exists():
        raise PlatformError(f"缺 {ACS_SCRIPT.name}(Acs-Token 生成器)")
    r = subprocess.run(["node", str(ACS_SCRIPT)], capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        raise PlatformError(
            f"Node 生成 Acs-Token 失败:{(r.stderr or r.stdout).strip()[:300]}"
            f"(百度改版的话重下 _acs/sdk/acs-2112.js)")
    return r.stdout.strip()


class BaijiahaoSession(ArticlePlatformSession):
    PLATFORM = "baijiahao"

    def __init__(self, cookie: str = "", **kwargs):
        super().__init__(cookie, **kwargs)
        self.token: str = ""          # JWT,从编辑页 HTML 抠
        self.acs: str = ""            # 一次性风控 token

    def _headers(self, with_acs: bool = False) -> dict:
        h = self.base_headers(
            Origin=BJH, Referer=EDIT_URL,
            **{"Content-Type": "application/x-www-form-urlencoded"})
        if self.token:
            h["token"] = self.token
        if with_acs and self.acs:
            h["Acs-Token"] = self.acs
        return h

    async def check_login(self) -> dict:
        """GET 编辑页抠 JWT。抠不到 = 登录态失效。"""
        r = await self.get(EDIT_URL, headers=self.base_headers(Referer=BJH))
        m = re.search(r"__BJH__INIT__AUTH__\s*=\s*['\"]([^'\"]+)", r.text)
        self.token = m.group(1) if m else ""
        if not self.token:
            raise AuthExpired("百家号登录态已失效(抠不到 __BJH__INIT__AUTH__),"
                              "请重新导入 Cookie")
        return {"token": self.token[:24] + "…",
                "has_bduss": "BDUSS" in self.parse_cookie(self.cookie)}

    async def post_form(self, path: str, data: dict, *, with_acs: bool = False,
                        timeout: int = 180) -> dict:
        r = await self.post(BJH + path, data=data, headers=self._headers(with_acs),
                            timeout=timeout)
        out = await self.json_or_raise(r, path)
        out["__status"] = r.status_code
        return out

    # ── 图片 ───────────────────────────────────────────────────────────────
    async def upload_image(self, p: Path) -> tuple[str, int, int]:
        """上传一张图 → (图床 url, w, h)。5MB 是平台上限。

        base64 值**以逗号开头**(前端把 dataURL 去前缀后残留的逗号,抓包原样如此)。
        这个接口只要 token,不要 Acs-Token。
        """
        if not p.exists():
            raise PlatformError(f"图片不存在:{p}")
        size_mb = p.stat().st_size / 1048576
        if size_mb > 5:
            raise PlatformError(
                f"{p.name} 有 {size_mb:.1f}MB,超过平台单图 5MB 上限,先压缩。"
                f"(动图伪装成 .jpg 是常见原因,抽首帧即可)")
        b64 = base64.b64encode(p.read_bytes()).decode()
        res = await self.post_form("/pcui/picture/processproxy",
                                   {"action[0]": "save", "base64": "," + b64},
                                   timeout=240)
        url = (res.get("ret") or {}).get("url")
        if not url:
            raise PlatformError(
                f"{p.name} 上传失败:{json.dumps(res, ensure_ascii=False)[:300]}")
        w, h = image_size(p)
        self.logger.info("[百家号] 图片已传 %s %sx%s %.2fMB", p.name, w, h, size_mb)
        return url, w, h

    # ── markdown → 百家号 HTML ─────────────────────────────────────────────
    async def md_to_html(self, md: str, base_dir: Path) -> tuple[str, list[str]]:
        """markdown → 百家号正文 HTML。返回 (html, 已上传图片 url 列表)。

        百家号的图片结构是「一个 <p> 包 <img>,紧跟一个 caption <p>」,
        <img> 必须带 data-w/data-h,否则版式会错(抓包实测)。
        """
        uploaded: list[str] = []
        imgs: dict[str, tuple[str, int, int]] = {}
        for _alt, src in IMG_RE.findall(md):
            if src.startswith(("http://", "https://")) or src in imgs:
                continue
            local = resolve_local(src, base_dir)
            if local is None or not local.exists():
                self.logger.warning("[百家号] 图片不存在,跳过:%s", src)
                continue
            url, w, h = await self.upload_image(local)
            imgs[src] = (url, w, h)
            uploaded.append(url)

        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def inline(s: str) -> str:
            """行内:加粗/斜体/行内码。顺序要紧——先粗后斜,否则 ** 会被 * 吃掉。"""
            s = esc(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
            s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
            return s

        def img_block(alt: str, src: str) -> str:
            """一张图 = 图片 <p> + 紧跟的图注 <p>,结构与抓包 1:1。

            ⚠️ `data-bjh-origin-src` 和 `data-bjh-params` 必须带:首版只给 src +
            data-w/h,编辑器里图片**不渲染**(图注出来了、图是空的)。
            """
            if src in imgs:
                url, w, h = imgs[src]
            else:
                url, w, h = src, 0, 0
            cap_id = f"cap-{abs(hash(url)) % 90000000 + 10000000}"
            params = urllib.parse.quote(
                json.dumps({"org_url": url, "ai_image_type": ""}, ensure_ascii=False),
                safe="")
            attrs = f' data-w="{w}" data-h="{h}"' if w else ""
            cap_attr = f' data-bjh-caption-text="{esc(alt)}"' if alt else ""
            out = (f'<p data-bjh-caption-id="{cap_id}" class="cant-justify"{cap_attr}>'
                   f'<img src="{url}" data-bjh-origin-src="{url}" data-bjh-type="IMG"'
                   f' data-bjh-params="{params}"{attrs}></p>')
            if alt:
                out += (f'<p class="bjh-image-caption" data-bjh-caption-length="{len(alt)}"'
                        f' data-bjh-caption-text="" data-bjh-caption-for="{cap_id}">'
                        f'{esc(alt)}</p>')
            return out

        html: list[str] = []
        lines = md.replace("\r\n", "\n").split("\n")
        i = 0
        while i < len(lines):
            ln = lines[i].rstrip()
            if not ln.strip():
                i += 1
                continue
            m = IMG_RE.fullmatch(ln.strip())
            if m:
                html.append(img_block(m.group(1), m.group(2)))
                i += 1
                continue
            # 标题 → 加粗独立段(百家号正文不用 h1/h2,抓包里小标题就是 <strong>)
            if m := re.match(r"^(#{1,6})\s+(.*)", ln):
                html.append(f"<p><strong>{inline(m.group(2))}</strong></p>")
                i += 1
                continue
            if ln.startswith("> "):
                html.append(f'<p style="text-indent: 2em;">{inline(ln[2:])}</p>')
                i += 1
                continue
            if re.match(r"^\s*([-*+]|\d+\.)\s+", ln):
                ordered = bool(re.match(r"^\s*\d+\.\s+", ln))
                items = []
                while i < len(lines) and re.match(r"^\s*([-*+]|\d+\.)\s+",
                                                  lines[i].rstrip()):
                    items.append(re.sub(r"^\s*([-*+]|\d+\.)\s+", "", lines[i].rstrip()))
                    i += 1
                tag = "ol" if ordered else "ul"
                html.append(f"<{tag}>"
                            + "".join(f"<li><p>{inline(x)}</p></li>" for x in items)
                            + f"</{tag}>")
                continue
            para = IMG_RE.sub(lambda mm: f"\x00{mm.group(1)}\x01{mm.group(2)}\x00", ln)
            if "\x00" in para:
                for part in para.split("\x00"):
                    if not part:
                        continue
                    if "\x01" in part:
                        a, s = part.split("\x01", 1)
                        html.append(img_block(a, s))
                    else:
                        html.append(f"<p>{inline(part)}</p>")
            else:
                html.append(f"<p>{inline(ln)}</p>")
            i += 1
        return "".join(html), uploaded

    @staticmethod
    def build_payload(title: str, html: str, cover_urls: list[str], *,
                      ai_generated: bool, reward: bool, auto_podcast: bool) -> dict:
        """发布 body(字段与抓包 1:1 对齐)。"""
        cover = [{"src": u, "cropData": {}, "machine_chooseimg": 0,
                  "isLegal": 0, "cover_source_tag": "local"} for u in cover_urls]
        edit_point = [{"img_type": t, "img_num": {"template": 0, "font": 0, "filter": 0,
                                                  "paster": 0, "cut": 0, "any": 0}}
                      for t in ("cover", "body")]
        plain = re.sub(r"<[^>]+>", "", html)
        return {
            "type": "news", "title": title, "subtitle": "", "content": html,
            "abstract_from": "1", "source": "upload", "cover_source": "upload",
            "cover_layout": "three" if len(cover_urls) == 3 else "one",
            "cover_images": json.dumps(cover, ensure_ascii=False),
            "_cover_images_map": json.dumps(
                [{"src": u, "origin_src": u} for u in cover_urls], ensure_ascii=False),
            "cover_image_source[wide_cover_image_source]": "local",
            "len": str(len(plain)), "isBeautify": "false", "usingImgFilter": "false",
            "source_reprinted_allow": "0", "first_exclusive_publish_v2": "3",
            "event2news": "0", "clue": "", "bjhmt": "", "order_id": "",
            "bjhtopic_id": "", "bjhtopic_info": "", "aigc_rebuild": "",
            # 服务端不校验,前端只用它的存在性打日志(webpack 模块 83075)
            "BJH_FE_NOUNCE": secrets.token_hex(16),
            "image_edit_point": json.dumps(edit_point),
            # ⚠️ 存草稿不回存这三项:草稿页看到的勾选状态来自平台配置模板而非提交值,
            #    所以「播客已关」只能在真发布时生效、也只能在发布后核对。
            "activity_list[0][id]": "ai_tts",
            "activity_list[0][is_checked]": "1" if auto_podcast else "0",
            "activity_list[1][id]": "reward",
            "activity_list[1][is_checked]": "1" if reward else "0",
            "activity_list[2][id]": "aigc_bjh_status",
            "activity_list[2][is_checked]": "1" if ai_generated else "0",
        }

    async def publish_article(self, draft: ArticleDraft, *,
                              publish: bool = False) -> ArticleResult:
        if len(draft.title) > 64:
            raise PlatformError(f"标题 {len(draft.title)} 字,超过平台上限 64")
        if not self.token:
            await self.check_login()

        if draft.content_html:
            html, body_imgs = draft.content_html, []
        else:
            html, body_imgs = await self.md_to_html(draft.markdown, draft.base_dir)

        # 封面:平台必填,只支持 1 张或 3 张
        if draft.covers:
            if len(draft.covers) not in (1, 3):
                raise PlatformError(
                    f"封面只能给 1 张(单图)或 3 张(三图),给了 {len(draft.covers)} 张")
            cover_urls = [(await self.upload_image(Path(p)))[0] for p in draft.covers]
        elif body_imgs:
            cover_urls = body_imgs[:3] if len(body_imgs) >= 3 else body_imgs[:1]
        else:
            raise PlatformError("封面是平台必填项:给 covers,或在正文里放至少一张本地图")

        payload = self.build_payload(
            draft.title, html, cover_urls,
            ai_generated=draft.ai_generated,
            reward=draft.extras.get("reward", True),
            auto_podcast=draft.extras.get("podcast", False))

        if publish:
            # Acs-Token 一次性(第二段是生成时刻毫秒戳),提交前才取
            self.acs = await asyncio.to_thread(node_acs)   # 同步 subprocess 别冻事件循环
            path = "/pcui/article/publish?type=news&callback=bjhpublish"
        else:
            path = "/pcui/article/save?type=news&callback=bjhdraft"

        # 提交(发布/存草稿)是非幂等写:失败重试可能重复建稿,且 Acs-Token 一次性
        async with self.no_retry():
            res = await self.post_form(path, payload, with_acs=publish, timeout=240)
        if res.get("errno") != 0:
            raise PlatformError(
                f"百家号提交未通过:{json.dumps(res, ensure_ascii=False)[:400]}")
        ret = res.get("ret") or {}
        aid = str(ret.get("id") or ret.get("article_id") or "")
        return ArticleResult(
            ok=True, article_id=aid, is_draft=not publish,
            url=f"https://baijiahao.baidu.com/s?id={ret.get('nid')}" if ret.get("nid") else "",
            message=res.get("errmsg") or "success", raw=res)

    async def delete_article(self, article_id: str) -> dict:
        """删除草稿/文章(参数名必须是 article_id,用 id 会回 10001001)。

        ⚠️ 别连着再调一次去验证——会撞 `10000008 您操作太频繁了`(限流不是失败),
        间隔 75s 再查。软删除:复查已删的返回 `20040028 文章当前状态不可进行此操作`,
        **不是** `20030000 文章不存在`,别拿后者当判据。
        """
        return await self.post_form("/pcui/article/remove", {"article_id": article_id})
