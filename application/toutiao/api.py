"""今日头条图文发布协议。纯 HTTP,不需要浏览器、不需要签名。

协议移植自 quote-video/scripts/toutiao_publish.py(2026-08-10/11 实测线),
下面每条结论都有对照实验,**别照猜想改**:

| 项 | 结论 | 怎么验的 |
|---|---|---|
| `_signature` | 服务端不校验 | 三组对照(旧算法生成/瞎编同长度/完全不带),响应全部 `code=7050`,都拦在业务层 |
| `a_bogus` / `msToken` | 同样不必带 | 上面三组都没带,照样进业务层 |
| 发文最小字段 | 只要 4 个:`title` `content` `source=29` `save` | 逐层加字段实验,第一层就 `code=0` |
| **`save` 语义** | **`save=1` 才是发布**,`save=0` 新建直接 `7050 保存失败` | ⚠️ **与百家号相反**。`save=1` 建出来 `is_draft=false` 且有公开链接;`save=0` 是编辑器更新已有草稿用的(那些请求都带 `pgc_id`) |
| 封面 | `pgc_feed_covers` + `draft_form_data` + `article_ad_type=3` 三件套 | ⚠️ 最小字段集实验漏了它,导致早期 17 篇发出去全是无封面 |
| 服务端只认封面 `uri` | `url` 留空照样生效 | 前端提交的 url/uri 是两个不同 hash,服务端回查时 origin_uri/thumb_url 全等于提交的 uri |

⚠️ 因为 `save=0` 新建不成立,这个平台**只有「发布」一条路**——所以
publish_article(publish=False) 会直接拒绝,而不是悄悄发出去。
"""
from __future__ import annotations

import html as _html
import json
import mimetypes
import re
import urllib.parse
import uuid
from pathlib import Path

from application.base import (ArticleDraft, ArticlePlatformSession, ArticleResult, AuthExpired, PlatformError, resolve_local)

TT = "https://mp.toutiao.com"
# 发布页固定 query(抓包逐字取)。_signature/msToken/a_bogus 实测不校验,不带。
PUB_Q = {"source": "mp", "type": "article", "aid": "1231", "mp_publish_ab_val": "0"}
COVER_RATIO = 3 / 2      # 信息流封面按 3:2 横图(抓包里编辑器给的是 1621×1080)


class ToutiaoSession(ArticlePlatformSession):
    PLATFORM = "toutiao"

    def __init__(self, cookie: str = "", **kwargs):
        super().__init__(cookie, **kwargs)
        self._uid: int = 0
        self._uname: str = ""

    def _headers(self, referer: str = f"{TT}/profile_v4/graphic/publish") -> dict:
        return self.base_headers(Origin=TT, Referer=referer)

    async def _api(self, path: str, data: dict | None = None, *,
                   base: str = TT, as_json: bool = False,
                   referer: str = f"{TT}/profile_v4/graphic/publish") -> dict:
        headers = self._headers(referer)
        if data is None:
            r = await self.get(base + path, headers=headers)
        elif as_json:
            headers["Content-Type"] = "application/json"
            r = await self.post(base + path, data=json.dumps(data), headers=headers)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            r = await self.post(base + path, data=urllib.parse.urlencode(data),
                                headers=headers)
        return await self.json_or_raise(r, path)

    async def check_login(self) -> dict:
        """取 UserId / UserName(图片上传要带)。取不到即视为登录态失效。"""
        d = (await self._api("/mp/agw/creator_center/user_info", None,
                             referer=f"{TT}/profile_v4/manage/graphic")).get("data") or {}
        uid = int(d.get("user_id") or d.get("id") or 0)
        if not uid:
            raise AuthExpired("今日头条登录态已失效,请重新导入 Cookie")
        self._uid = uid
        self._uname = d.get("screen_name") or d.get("name") or ""
        return {"uid": uid, "nickname": self._uname}

    # ── 图片 ───────────────────────────────────────────────────────────────
    async def upload_image(self, path: Path, source: str = "20020002") -> dict:
        """本地图直传 → 图床信息 dict。走编辑器同一个 multipart 接口。

        2026-08-10 实测:**不需要 `x-secsdk-csrf-token`**(浏览器会带但服务端不校验,
        和 publish 不校验 `_signature` 是同一回事)。
        source:正文图 20020002 / 封面 20020003——实测两者返回结构完全一致,
        分开只为跟浏览器行为一致。
        """
        if not path.exists():
            raise PlatformError(f"图片不存在:{path}")
        ctype = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        boundary = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        r = await self.post(
            f"{TT}/spice/image?upload_source={source}&aid=1231&device_platform=web",
            data=body,
            headers=self.base_headers(
                Origin=TT, Referer=f"{TT}/profile_v4/graphic/publish",
                **{"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Accept": "*/*"}),
            timeout=180)
        d = await self.json_or_raise(r, "/spice/image")
        img = d.get("data") or {}
        if d.get("code") != 0 or not img.get("image_url"):
            raise PlatformError(
                f"图片上传失败 {path.name}:{json.dumps(d, ensure_ascii=False)[:140]}")
        self.logger.info("[头条] 图片已传 %s → %sx%s", path.name,
                         img.get("image_width"), img.get("image_height"))
        return img

    async def upload_by_url(self, src: str) -> str:
        """让头条服务端去拉公网图片 URL。

        ⚠️ 只对能公开拉取的图源有效:图虫(自家图库)可以,**微博图床拉不到**(防盗链,
        回 `StatusCode:1 上传失败`)。所以本地文件一律走 upload_image。
        """
        payload = {"params": {"ParamsList": [{"ImageSourceUrl": src, "UserId": self._uid,
                                              "UserName": self._uname, "Watermark": "0",
                                              "Platform": "toutiaohao"}],
                              "search_id": "00000000-0000-4000-8000-000000000000"}}
        r = await self._api("/api/uploadbyurls", payload,
                            base="https://dficimage.toutiao.com", as_json=True,
                            referer=f"{TT}/")
        det = ((r.get("data") or {}).get("DetailsList") or [{}])[0]
        if det.get("StatusCode") == 0 and det.get("ImageUrl"):
            return det["ImageUrl"]
        self.logger.warning("[头条] 服务端拉图失败(%s):%s", det.get("StatusMessage"), src[:70])
        return ""

    @staticmethod
    def crop_cover(path: Path, out_dir: Path) -> Path:
        """居中裁成 3:2 横图。

        为什么必须裁:管线正文图常是竖图(3584×4800),信息流里会被平台自己裁成中间
        一条、主体被切掉。抓包显示编辑器自己也裁。缺 Pillow 时返回原图并出声——
        不静默降级。
        """
        try:
            from PIL import Image
        except ImportError:
            return path
        im = Image.open(path).convert("RGB")
        w, h = im.size
        if abs(w / h - COVER_RATIO) < 0.02:
            return path
        if w / h > COVER_RATIO:
            nw, nh = int(h * COVER_RATIO), h
        else:
            nw, nh = w, int(w / COVER_RATIO)
        left, top = (w - nw) // 2, min((h - nh) // 2, int(h * 0.15))
        out = out_dir / f"cover-{path.stem}.jpg"
        im.crop((left, top, left + nw, top + nh)).save(out, quality=92)
        return out

    @staticmethod
    def build_covers(img: dict) -> list[dict]:
        """拼 pgc_feed_covers(url 留空,服务端只认 uri)。"""
        return [{"id": "", "url": "", "uri": img["image_uri"], "ic_uri": "",
                 "thumb_width": img.get("image_width", 0),
                 "thumb_height": img.get("image_height", 0),
                 "extra": {"from_content_uri": "", "from_content": "0"}}]

    # ── markdown → 头条 HTML ───────────────────────────────────────────────
    async def md_to_html(self, md: str, base_dir: Path) -> str:
        """转成头条编辑器那套 HTML(正文段落固定 class="pgc-p",抓包逐字对齐)。"""
        out: list[str] = []
        for block in [b.strip() for b in md.split("\n\n") if b.strip()]:
            if block.startswith("# "):
                out.append(f'<h1 class="pgc-h-forward-slash">'
                           f'{_html.escape(block[2:].strip())}</h1>')
                continue
            m = re.fullmatch(r"!\[(.*?)\]\((.+?)\)", block)
            if m:
                alt, src = m.group(1), m.group(2)
                local = resolve_local(src, base_dir)
                if local is None:
                    url = await self.upload_by_url(src)
                elif local.exists():
                    url = (await self.upload_image(local)).get("image_url", "")
                else:
                    self.logger.warning("[头条] 图片不存在,跳过:%s", local)
                    url = ""
                if url:
                    out.append(f'<div class="pgc-img"><img src="{_html.escape(url)}"/>'
                               + (f'<p class="pgc-img-caption">{_html.escape(alt)}</p>'
                                  if alt else "")
                               + "</div>")
                continue
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _html.escape(block))
            out.append(f'<p class="pgc-p">{text}</p>')
        return "".join(out)

    # ── 发布 ───────────────────────────────────────────────────────────────
    async def publish_article(self, draft: ArticleDraft, *,
                              publish: bool = False) -> ArticleResult:
        if not publish:
            # save=0 新建实测直接 7050,平台没有「新建草稿」这条路。与其发出去,不如拒绝。
            raise PlatformError(
                "今日头条不支持新建草稿(save=0 新建会 7050 保存失败),"
                "要发请显式 publish=True")
        if not self._uid:
            await self.check_login()

        content = draft.content_html or await self.md_to_html(draft.markdown, draft.base_dir)
        fields = {
            "title": draft.title[:64],
            "content": content,
            "source": "29",
            "save": "1",          # ⚠️ 1=发布 / 0=更新已有草稿,与百家号相反
        }

        cover_src = draft.covers[0] if draft.covers else None
        if cover_src and Path(cover_src).exists():
            cropped = self.crop_cover(Path(cover_src), Path(cover_src).parent)
            img = await self.upload_image(cropped, source="20020003")
            # 三个字段是一套,抓包里同时出现
            fields["pgc_feed_covers"] = json.dumps(self.build_covers(img),
                                                   ensure_ascii=False)
            fields["draft_form_data"] = json.dumps({"coverType": 2})
            fields["article_ad_type"] = "3"

        fields.update(draft.extras.get("fields") or {})
        # 发布是非幂等写:超时后服务端可能已受理,盲目重试有重复发文风险
        async with self.no_retry():
            r = await self._api("/mp/agw/article/publish?" + urllib.parse.urlencode(PUB_Q),
                                fields)
        if r.get("code") != 0:
            raise PlatformError(f"头条发布失败:{json.dumps(r, ensure_ascii=False)[:300]}")
        pgc_id = str((r.get("data") or {}).get("pgc_id") or "")
        return ArticleResult(
            ok=True, article_id=pgc_id,
            url=f"https://www.toutiao.com/item/{pgc_id}/" if pgc_id else "",
            message=r.get("message") or "提交成功", raw=r)

    async def list_articles(self, size: int = 20) -> list[dict]:
        r = await self._api(f"/mp/agw/article/list?status=all&size={size}&page=1", None,
                            referer=f"{TT}/profile_v4/manage/graphic")
        return (r.get("data") or {}).get("content") or []

    async def delete_article(self, pgc_id: str) -> dict:
        """删除一篇(参数名必须是 pgc_id,用 id 会回 20007124 Id invalid)。"""
        return await self._api("/mp/agw/article/delete", {"pgc_id": pgc_id},
                               referer=f"{TT}/profile_v4/manage/graphic")
