"""微信公众号图文发布协议(纯 HTTP)。

协议移植自 quote-video/scripts/wechat_publish.py(2026-08-11 实测线)。

## 凭证是两样,缺一不可

| 凭证 | 来源 | 说明 |
|------|------|------|
| cookie | `mp.weixin.qq.com` 域的 Cookie | 登录一次导出 |
| token | **不在 cookie 里**,是登录时生成的会话参数 | 从后台页面 URL 的 `token=` 抠 |

只带 cookie 不带 token 访问 `/cgi-bin/home`,页面 14977 字节且 `uin:""` = 未登录;
带上 token 后 113910 字节、`uin` 有值。所以本类要求 cookie 和 token 同时给。

## ⚠️ 只能到草稿

群发(`masssend`)要管理员**扫码**:`code` 来自安全助手二维码,服务端只认扫过并确认过的,
参数糊弄不过去。且未认证订阅号一天只能群发 1 次、发错不能撤。
所以本模块**只实现到草稿**,群发留给人在手机上做。publish_article() 无论 publish
传什么都返回 is_draft=True。

## 两个必须做对的地方

1. **封面必须裁**:不裁会在发布时被 `masssend` 回 `ret=64004`「2.35:1 封面规格异常」。
   竖版卡片图直接当封面一定不合法——微信要 2.35:1 和 1:1 两个版本。
   先问 `crop_suggestion` 要建议框(它给的一定合法),再用 `crop_multi` 生成。
   这个接口会**间歇性回 system error**(同一次运行里第 1 条失败、第 2 条成功,参数一样),
   所以必须重试,不能放过去。
2. **摘要留空要两个字段一起**:`digest` 留空但 `auto_gen_digest=1` 的话,
   微信会自己拿正文开头补一段。
"""
from __future__ import annotations

import json
import os
import re
import asyncio
import time
import urllib.parse
import uuid as _uuid
from pathlib import Path

from application.base import (ArticleDraft, ArticlePlatformSession, ArticleResult, AuthExpired, PlatformError, resolve_local)

MP = "https://mp.weixin.qq.com"
# 抓包里的固定串,不是签名,不随请求变
FINGERPRINT = "125de2cbc982e84047cd279221436784"


def _rand() -> float:
    """抓包里 random 是 0-1 随机小数,纯防缓存,值本身不校验。"""
    return int.from_bytes(os.urandom(4), "big") / 2 ** 32


class WechatMpSession(ArticlePlatformSession):
    PLATFORM = "wechat_mp"

    def __init__(self, cookie: str = "", token: str = "", **kwargs):
        super().__init__(cookie, **kwargs)
        self.token = (token or "").strip()
        self.sess: dict = {}          # uin / ticket / gh / nick

    def _headers(self, **extra) -> dict:
        return self.base_headers(
            Origin=MP,
            Referer=f"{MP}/cgi-bin/appmsg?t=media/appmsg_edit&token={self.token}&lang=zh_CN",
            **{"X-Requested-With": "XMLHttpRequest"}, **extra)

    async def _api(self, path: str, data: dict | None = None, *,
                   query: dict | None = None) -> dict:
        """urlencoded 的 cgi-bin 接口。data 为 None 时是 GET。"""
        q = {"token": self.token, "lang": "zh_CN"}
        q.update(query or {})
        url = f"{MP}{path}?{urllib.parse.urlencode(q)}"
        if data is None:
            r = await self.get(url, headers=self._headers())
        else:
            payload = {"token": self.token, "lang": "zh_CN", "f": "json", "ajax": "1",
                       "fingerprint": FINGERPRINT, "random": str(_rand())}
            payload.update(data)
            r = await self.post(url, data=urllib.parse.urlencode(payload),
                                headers=self._headers(
                                    **{"Content-Type":
                                       "application/x-www-form-urlencoded; charset=UTF-8"}))
        return await self.json_or_raise(r, path)

    async def check_login(self) -> dict:
        """从后台首页解析 ticket / gh / 昵称——传图要 ticket。

        判据是页面长度 + uin:研究过,未登录时会回落到 14977 字节的精简版且 uin 为空。
        """
        if not self.token:
            raise AuthExpired("缺 token(不在 cookie 里,要从后台页面 URL 的 token= 抠)")
        r = await self.get(f"{MP}/cgi-bin/home?t=home/index&token={self.token}&lang=zh_CN",
                           headers=self.base_headers(Referer=MP + "/"))
        html = r.text
        if len(html) < 30000 or re.search(r'uin:\s*""\s*\|\|', html):
            raise AuthExpired("公众号登录态已失效(页面回落到未登录版本),请重新导入 Cookie+token")

        def pick(pat: str, default: str = "") -> str:
            m = re.search(pat, html)
            return m.group(1) if m else default

        self.sess = {
            "uin": pick(r'uin:\s*"(\d+)"'),
            "ticket": pick(r'ticket:\s*"([0-9a-f]{20,})"'),
            "gh": pick(r'user_name:\s*"(gh_\w+)"'),
            "nick": pick(r'nick_name:\s*"([^"]{1,32})"'),
        }
        return {"nickname": self.sess["nick"], "gh": self.sess["gh"],
                "uin": self.sess["uin"]}

    # ── 图片 ───────────────────────────────────────────────────────────────
    async def upload_image(self, path: Path) -> tuple[str, str]:
        """本地图 → 公众号图床。返回 (file_id, cdn_url)。multipart 字段名是 `file`。"""
        if not self.sess:
            await self.check_login()
        data = path.read_bytes()
        ext = path.suffix.lower().lstrip(".") or "png"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif"}.get(ext, "png")
        b = "----WebKitFormBoundary" + _uuid.uuid4().hex[:16]
        body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{path.name}"\r\nContent-Type: image/{mime}\r\n\r\n').encode() \
            + data + f"\r\n--{b}--\r\n".encode()
        q = {"action": "upload_material", "f": "json", "scene": "8",
             "writetype": "doublewrite", "groupid": "1",
             "ticket_id": self.sess["gh"], "ticket": self.sess["ticket"],
             "svr_time": str(int(time.time())), "token": self.token, "lang": "zh_CN",
             "seq": str(int(time.time() * 1000)), "t": str(_rand())}
        r = await self.post(f"{MP}/cgi-bin/filetransfer?{urllib.parse.urlencode(q)}",
                            data=body,
                            headers=self._headers(
                                **{"Content-Type": f"multipart/form-data; boundary={b}"}),
                            timeout=180)
        res = await self.json_or_raise(r, "/cgi-bin/filetransfer")
        if res.get("base_resp", {}).get("ret") != 0:
            raise PlatformError(f"传图失败 {path.name}:{res}")
        return str(res["content"]), res["cdn_url"]

    async def crop_cover(self, cdn: str) -> dict:
        """把封面裁成微信要的 2.35:1 和 1:1 两个版本。

        不做这步发布时会被 64004 拦。crop_suggestion/crop_multi 都会间歇性回
        system error,所以两段都带重试。
        """
        sug, W, H, boxes = {}, 0, 0, []
        for attempt in range(1, 4):
            sug = await self._api(
                "/cgi-bin/cropimage",
                {"data": json.dumps({"url": cdn, "ratio_type": [3, 1, 2, 4]},
                                    ensure_ascii=False), "timeout": "10000"},
                query={"action": "crop_suggestion"})
            W, H = sug.get("original_width") or 0, sug.get("original_height") or 0
            boxes = [s for s in sug.get("suggestion", []) if s.get("is_valid")]
            if W and H and boxes:
                break
            self.logger.warning("[公众号] 裁剪建议第 %d 次失败:%s", attempt,
                                str(sug.get("base_resp"))[:60])
            if attempt < 3:
                await asyncio.sleep(3 * attempt)
        if not (W and H and boxes):
            self.logger.warning("[公众号] 三次都拿不到裁剪建议,这条发布时会被 64004 拦")
            return {}

        def pick(target: float) -> dict:
            b = min(boxes, key=lambda s: abs(s["crop_width"] / max(1, s["crop_height"]) - target))
            return {"x1": b["crop_x"] / W, "y1": b["crop_y"] / H,
                    "x2": (b["crop_x"] + b["crop_width"]) / W,
                    "y2": (b["crop_y"] + b["crop_height"]) / H}

        a, b1 = pick(2.35), pick(1.0)
        data = {"imgurl": cdn, "size_count": "2",
                "size0_x1": a["x1"], "size0_y1": a["y1"], "size0_x2": a["x2"],
                "size0_y2": a["y2"], "format0": "2.35_1",
                "size1_x1": b1["x1"], "size1_y1": b1["y1"], "size1_x2": b1["x2"],
                "size1_y2": b1["y2"], "format1": "1_1"}
        r = await self._api("/cgi-bin/cropimage", data, query={"action": "crop_multi"})
        res = r.get("result") or []
        for attempt in range(1, 3):
            if len(res) >= 2:
                break
            await asyncio.sleep(3 * attempt)
            r = await self._api("/cgi-bin/cropimage", data, query={"action": "crop_multi"})
            res = r.get("result") or []
        if len(res) < 2:
            self.logger.warning("[公众号] 封面裁剪失败(重试后仍失败):%s", str(r)[:110])
            return {}
        return {"c235": res[0]["cdnurl"], "c11": res[1]["cdnurl"]}

    # ── markdown → 公众号 HTML ─────────────────────────────────────────────
    async def md_to_html(self, md: str, base_dir: Path,
                         covers: list | None = None) -> str:
        """转成公众号编辑器那套 HTML(`<section><span leaf="">`,抓包逐字对齐)。

        ⚠️ 别在开头加空 section:抓包样本里那个 `<br>` 是人在编辑器里手打的空行,
        不是格式要求,照抄会让每篇正文第一行都空一行。
        """
        out: list[str] = []
        for raw in md.split("\n"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"!\[[^\]]*\]\(([^)]+)\)", line)
            if m:
                src = m.group(1)
                p = resolve_local(src, base_dir)
                if p is None:
                    self.logger.warning("[公众号] 跳过网络图(要本地文件):%s", src[:60])
                    continue
                if not p.exists():
                    self.logger.warning("[公众号] 跳过找不到的图:%s", src)
                    continue
                fid, cdn = await self.upload_image(p)
                if covers is not None:
                    covers.append(cdn)          # 第一张给封面用
                ext = p.suffix.lower().lstrip(".").replace("jpg", "jpeg")
                out.append(
                    f'<section style="text-align: center;" nodeleaf=""><img '
                    f'class="rich_pages wxw-img" data-type="{ext}" type="block" '
                    f'data-imgfileid="{fid}" data-upload="1" data-src="{cdn}"></section>')
                continue
            # **01** 这类纯数字小标题 → 居中红底白字(对标账号的版式)
            mk = re.fullmatch(r"\*\*(\d{2})\*\*", line)
            if mk:
                out.append(
                    '<section style="text-align: center;"><span leaf="" '
                    'style="color: rgb(255, 255, 255);background-color: rgb(255, 0, 0);'
                    f'font-size: 15px;">{mk.group(1)}</span></section>')
                continue
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            out.append(f'<section><span leaf="">{line}</span></section>')
        out.append('<p style="display: none;"><mp-style-type data-value="3">'
                   '</mp-style-type></p>')
        return "".join(out)

    # ── 草稿 ───────────────────────────────────────────────────────────────
    @staticmethod
    def _multipart(fields: dict) -> tuple[bytes, str]:
        b = "----WebKitFormBoundary" + _uuid.uuid4().hex[:16]
        buf = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
               for k, v in fields.items()]
        buf.append(f"--{b}--\r\n")
        return "".join(buf).encode("utf-8"), b

    async def save_draft(self, arts: list[dict], appmsgid: str = "") -> str:
        """建草稿(appmsgid 空)或更新草稿。

        `arts` 是文章列表——一条群发可带多条(主条+次条,最多 8 条)。
        ⚠️ 抓包里所有字段都带 `0` 后缀(title0/content0/…),**这个 0 是文章索引**,
        第二条写 title1/content1,`count`/`articlenum`/`req.idx_infos` 跟着变。
        每篇的键:title / content / digest / author / cover / crops / original
        """
        n = len(arts)
        f = {
            "token": self.token, "lang": "zh_CN", "f": "json", "ajax": "1",
            "fingerprint": FINGERPRINT, "random": str(_rand()),
            "AppMsgId": appmsgid, "count": str(n), "data_seq": "null",
            "operate_from": "Chrome", "isnew": "0", "articlenum": str(n),
            "pre_timesend_set": "0",
            "req": '{"idx_infos":[' + ",".join(
                '{"save_old":0,"cps_info":{"cps_import":0}}' for _ in arts) + "]}",
            "is_auto_type_setting": "3", "save_type": "1", "isneedsave": "0",
        }
        for i, a in enumerate(arts):
            crops, cover = a.get("crops") or {}, a.get("cover", "")
            ori = a.get("original")
            f.update({
                f"title{i}": a["title"], f"author{i}": a.get("author", ""),
                f"digest{i}": a.get("digest", ""),
                # ⚠️ 两个都得是 0 才能真空着,否则微信拿正文开头自动补摘要
                f"auto_gen_digest{i}": "0",
                f"content{i}": a["content"], f"sourceurl{i}": "",
                f"need_open_comment{i}": "1", f"only_fans_can_comment{i}": "0",
                f"reply_flag{i}": "2", f"auto_elect_comment{i}": "1",
                f"auto_elect_reply{i}": "1", f"option_version{i}": "5",
                f"open_fansmsg{i}": "0", f"show_cover_pic{i}": "0",
                f"cdn_url{i}": cover, f"cdn_url_back{i}": cover,
                # 这两个不填,发布时会被拦成 64004
                f"cdn_235_1_url{i}": crops.get("c235", ""),
                f"cdn_1_1_url{i}": crops.get("c11", ""),
                f"copyright_type{i}": "1" if ori else "0",
                f"releasefirst{i}": "1" if ori else "",
                f"reprint_permit_type{i}": "1" if ori else "",
                f"copyright_img_list{i}": '{"max_width":586,"img_list":[]}',
                f"share_page_type{i}": "0", f"share_imageinfo{i}": '{"list":[]}',
                f"dot{i}": "{}", f"categories_list{i}": "[]",
                f"appmsg_album_info{i}": '{"appmsg_album_infos":[]}',
                f"can_insert_ad{i}": "1", f"open_comment_ad{i}": "1",
                f"audio_info{i}": '{"audio_infos":[]}',
                f"mp_video_info{i}": '{"list":[]}',
                f"style_type{i}": "3", f"new_pic_process{i}": "0",
                f"disable_recommend{i}": "0", f"is_finder_video{i}": "0",
                f"finder_draft_id{i}": "0", f"applyori{i}": "0", f"can_reward{i}": "0",
                f"pay_gifts_count{i}": "0", f"is_video_recommend{i}": "-1",
                f"writerid{i}": "0", f"last_choose_cover_from{i}": "0",
                f"app_cover_auto{i}": "0", f"allow_fast_reprint{i}": "0", f"fee{i}": "0",
                f"is_share_copyright{i}": "0", f"is_pay_subscribe{i}": "0",
                f"is_cartoon_copyright{i}": "0", f"danmu_pub_type{i}": "0",
                f"is_set_sync_to_finder{i}": "0", f"import_to_finder{i}": "0",
                f"incontent_ad_count{i}": "0", f"multi_picture_cover{i}": "0",
                f"title_gen_type{i}": "0", f"open_keyword_ad{i}": "0",
                f"is_user_no_claim_source{i}": "0",
            })
        body, b = self._multipart(f)
        sub = "update" if appmsgid else "create"
        r = await self.post(
            f"{MP}/cgi-bin/operate_appmsg?t=ajax-response&sub={sub}&type=77"
            f"&token={self.token}&lang=zh_CN",
            data=body,
            headers=self._headers(
                **{"Content-Type": f"multipart/form-data; boundary={b}"}),
            timeout=180)
        res = await self.json_or_raise(r, "/cgi-bin/operate_appmsg")
        ret = res.get("base_resp", {}).get("ret")
        if ret not in (0, None):
            raise PlatformError(f"存草稿失败:{res}")
        return str(res.get("appMsgId") or appmsgid)

    async def publish_article(self, draft: ArticleDraft, *,
                              publish: bool = False) -> ArticleResult:
        """建草稿。**群发要扫码,本模块不做**,所以 publish 参数被忽略。"""
        if not self.sess:
            await self.check_login()
        covers: list[str] = []
        content = draft.content_html or await self.md_to_html(
            draft.markdown, draft.base_dir, covers)
        cover_cdn = ""
        if draft.covers:
            _fid, cover_cdn = await self.upload_image(Path(draft.covers[0]))
        elif covers:
            cover_cdn = covers[0]
        crops = await self.crop_cover(cover_cdn) if cover_cdn else {}

        async with self.no_retry():          # 建稿是写提交,重试可能重复建稿
            appmsgid = await self.save_draft([{
                "title": draft.title, "content": content,
                "digest": draft.digest, "author": draft.author,
                "cover": cover_cdn, "crops": crops,
                "original": draft.extras.get("original", True),
            }])
        return ArticleResult(
            ok=True, article_id=appmsgid, is_draft=True,
            message="草稿已保存(群发需管理员扫码,请在手机端确认发送)",
            raw={"appMsgId": appmsgid})
