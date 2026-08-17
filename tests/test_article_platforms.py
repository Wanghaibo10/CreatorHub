"""图文平台(百家号/今日头条/微信公众号)协议层测试。

不联网:所有 HTTP 都打桩。重点守三件容易回归的事——
  1. 三个平台的 HTML 产物格式(都是照抓包 1:1 对齐的,改坏了平台版式就错)
  2. `save` 语义不能搞反(头条 save=1 才是发布,百家号 save 是草稿)
  3. 错误分类要能喂给既有风控闸门
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from application.base import (ArticleDraft, ArticlePlatformSession, AuthExpired, PlatformError, resolve_local)
from application.baijiahao import BaijiahaoSession
from application.registry import (ALL_KEYS, ARTICLE_KEYS, COOKIE_LOGIN_KEYS, SPECS, normalize, spec)
from application.toutiao import ToutiaoSession
from application.wechat_mp import WechatMpSession, split_creds
from moss.core.risk import RiskCategory, classify_platform_error


def run(coro):
    return asyncio.run(coro)


class RegistryTests(unittest.TestCase):
    def test_article_platforms_registered(self):
        self.assertEqual(ARTICLE_KEYS, ("baijiahao", "toutiao", "wechat_mp"))
        for key in ARTICLE_KEYS:
            s = spec(key)
            self.assertEqual(s.kind, "article")
            self.assertEqual(s.publish_via, "http")
            # 图文平台都不支持监控他人作品,别被默认值带进监控调度
            self.assertFalse(s.can_monitor_others)
            self.assertIn("cookie", s.login_methods)

    def test_unknown_platform_falls_back(self):
        self.assertEqual(spec("nope").key, "douyin")
        self.assertEqual(normalize("nope"), "douyin")
        self.assertEqual(normalize("toutiao"), "toutiao")
        # 限定白名单时不在表里的要被打回默认
        self.assertEqual(normalize("xhs", allowed=COOKIE_LOGIN_KEYS), "xhs")

    def test_specs_cover_all_keys(self):
        for key in ALL_KEYS:
            s = SPECS[key]
            self.assertTrue(s.label and s.host and s.home_url and s.cookie_domain)


class ErrorClassificationTests(unittest.TestCase):
    """错误要能被既有风控闸门识别,否则冷却/计数全落空。"""

    def test_auth_expired_maps_to_auth(self):
        cat, sig = classify_platform_error(AuthExpired())
        self.assertEqual(cat, RiskCategory.AUTH)
        self.assertEqual(sig, "logged_out")

    def test_status_code_drives_risk(self):
        for code in (403, 429, 461, 471):
            cat, sig = classify_platform_error(PlatformError("x", status_code=code))
            self.assertEqual(cat, RiskCategory.RISK, f"http {code} 应判风控")
            self.assertEqual(sig, f"http_{code}")

    def test_plain_error_is_business(self):
        # 回归守卫:PlatformError 默认不能挂 category,否则会盖掉 status_code 判断
        cat, _ = classify_platform_error(PlatformError("标题不能为空"))
        self.assertEqual(cat, RiskCategory.BUSINESS)


class BaseHelperTests(unittest.TestCase):
    def test_parse_cookie(self):
        self.assertEqual(ArticlePlatformSession.parse_cookie("a=1; b=2; =3"),
                         {"a": "1", "b": "2"})

    def test_resolve_local(self):
        # 相对路径要按 base_dir 解析;用 resolve() 后的 base 比对,
        # 否则 macOS 上 /tmp -> /private/tmp 的软链会让断言假失败
        base = Path("/tmp").resolve()
        self.assertIsNone(resolve_local("https://x.com/a.jpg", base))
        self.assertEqual(resolve_local("/abs/a.jpg", base), Path("/abs/a.jpg"))
        self.assertEqual(resolve_local("a.jpg", base), base / "a.jpg")

    def test_cookie_in_headers(self):
        s = ToutiaoSession("a=1")
        self.assertEqual(s.base_headers()["Cookie"], "a=1")
        self.assertNotIn("Cookie", ToutiaoSession("").base_headers())


class ToutiaoTests(unittest.TestCase):
    def setUp(self):
        self.api = ToutiaoSession("sessionid=x")
        self.api._uid, self.api._uname = 123, "u"

    def test_html_matches_editor_format(self):
        md = "# 标题\n\n正文**粗**。"
        html = run(self.api.md_to_html(md, Path(".")))
        self.assertIn('<h1 class="pgc-h-forward-slash">标题</h1>', html)
        self.assertIn('<p class="pgc-p">正文<strong>粗</strong>。</p>', html)

    def test_html_escapes_injection(self):
        html = run(self.api.md_to_html("<script>x</script>", Path(".")))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_draft_is_refused_not_silently_published(self):
        """save=0 新建实测 7050,平台只有发布一条路——必须拒绝而不是悄悄发出去。"""
        with self.assertRaises(PlatformError) as cm:
            run(self.api.publish_article(ArticleDraft(title="t", markdown="x"),
                                         publish=False))
        self.assertIn("不支持新建草稿", str(cm.exception))

    def test_publish_sends_save_1(self):
        captured = {}

        async def fake_api(path, data=None, **kw):
            captured["path"], captured["data"] = path, data
            return {"code": 0, "message": "提交成功", "data": {"pgc_id": "999"}}

        self.api._api = fake_api
        r = run(self.api.publish_article(
            ArticleDraft(title="标题", markdown="正文"), publish=True))
        self.assertTrue(r.ok)
        self.assertEqual(r.article_id, "999")
        self.assertIn("toutiao.com/item/999", r.url)
        # ⚠️ 与百家号相反:1=发布。搞反会把草稿直接发到实名号上
        self.assertEqual(captured["data"]["save"], "1")
        self.assertEqual(captured["data"]["source"], "29")

    def test_publish_failure_raises(self):
        self.api._api = AsyncMock(return_value={"code": 7050, "message": "保存失败"})
        with self.assertRaises(PlatformError):
            run(self.api.publish_article(ArticleDraft(title="t", markdown="x"),
                                         publish=True))

    def test_cover_fields_come_as_a_set(self):
        """封面三件套必须同时出现,少一个就是无封面文章(早期 17 篇的坑)。"""
        captured = {}

        async def fake_api(path, data=None, **kw):
            captured.update(data or {})
            return {"code": 0, "data": {"pgc_id": "1"}}

        self.api._api = fake_api
        self.api.upload_image = AsyncMock(return_value={
            "image_uri": "uri-1", "image_width": 900, "image_height": 600})
        self.api.crop_cover = staticmethod(lambda p, d: p)
        tmp = Path(__file__).parent / "_cover_stub.jpg"
        tmp.write_bytes(b"\xff\xd8stub")
        try:
            run(self.api.publish_article(
                ArticleDraft(title="t", markdown="正文", covers=[tmp]), publish=True))
        finally:
            tmp.unlink(missing_ok=True)
        self.assertIn("pgc_feed_covers", captured)
        self.assertEqual(captured["draft_form_data"], json.dumps({"coverType": 2}))
        self.assertEqual(captured["article_ad_type"], "3")
        cover = json.loads(captured["pgc_feed_covers"])[0]
        self.assertEqual(cover["uri"], "uri-1")
        self.assertEqual(cover["url"], "")      # 服务端只认 uri,url 留空


class BaijiahaoTests(unittest.TestCase):
    def setUp(self):
        self.api = BaijiahaoSession("BDUSS=x")
        self.api.token = "jwt"

    def test_html_structure(self):
        html = run(self.api.md_to_html(
            "# 小标题\n\n正文**粗**\n\n> 引用\n\n- 甲\n- 乙", Path(".")))[0]
        # 百家号正文不用 h1/h2,小标题是加粗段
        self.assertIn("<p><strong>小标题</strong></p>", html)
        self.assertNotIn("<h1", html)
        self.assertIn('<p style="text-indent: 2em;">引用</p>', html)
        self.assertIn("<ul><li><p>甲</p></li><li><p>乙</p></li></ul>", html)

    def test_title_limit(self):
        with self.assertRaises(PlatformError):
            run(self.api.publish_article(
                ArticleDraft(title="超" * 65, markdown="x"), publish=True))

    def test_cover_count_must_be_1_or_3(self):
        with self.assertRaises(PlatformError) as cm:
            run(self.api.publish_article(
                ArticleDraft(title="t", markdown="x",
                             covers=[Path("a"), Path("b")]), publish=True))
        self.assertIn("1 张", str(cm.exception))

    def test_payload_layout_and_switches(self):
        one = self.api.build_payload("t", "<p>x</p>", ["u1"], ai_generated=True,
                                     reward=True, auto_podcast=False)
        three = self.api.build_payload("t", "<p>x</p>", ["a", "b", "c"],
                                       ai_generated=False, reward=False,
                                       auto_podcast=True)
        self.assertEqual(one["cover_layout"], "one")
        self.assertEqual(three["cover_layout"], "three")
        # AIGC 声明默认开;播客默认不勾(刻意与编辑器 UI 默认相反)
        self.assertEqual(one["activity_list[2][is_checked]"], "1")
        self.assertEqual(one["activity_list[0][is_checked]"], "0")
        self.assertEqual(three["activity_list[2][is_checked]"], "0")
        self.assertEqual(three["activity_list[0][is_checked]"], "1")
        # len 是纯文长度,不含标签
        self.assertEqual(one["len"], "1")

    def test_draft_vs_publish_path(self):
        """百家号:save=草稿、publish=发布(与头条相反)。"""
        seen = []

        async def fake_post(path, data, **kw):
            seen.append(path)
            return {"errno": 0, "errmsg": "success", "ret": {"id": "7"}}

        self.api.post_form = fake_post
        self.api.md_to_html = AsyncMock(return_value=("<p>x</p>", ["http://img/1"]))
        r = run(self.api.publish_article(ArticleDraft(title="t", markdown="x"),
                                         publish=False))
        self.assertTrue(r.is_draft)
        self.assertIn("/pcui/article/save", seen[0])

    def test_image_size_reads_png(self):
        from application.baijiahao.api import image_size
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
               + (320).to_bytes(4, "big") + (240).to_bytes(4, "big"))
        p = Path(__file__).parent / "_size_stub.png"
        p.write_bytes(png)
        try:
            self.assertEqual(image_size(p), (320, 240))
        finally:
            p.unlink(missing_ok=True)


class WechatMpTests(unittest.TestCase):
    def setUp(self):
        self.api = WechatMpSession("a=1", token="tok")
        self.api.sess = {"uin": "1", "ticket": "t", "gh": "gh_x", "nick": "n"}

    def test_split_creds(self):
        self.assertEqual(split_creds("ck=1||99"), ("ck=1", "99"))
        self.assertEqual(split_creds("ck=1"), ("ck=1", ""))

    def test_missing_token_is_auth_error(self):
        api = WechatMpSession("a=1")           # 没给 token
        with self.assertRaises(AuthExpired):
            run(api.check_login())

    def test_html_section_format(self):
        html = run(self.api.md_to_html("**01**\n\n正文**粗**", Path(".")))
        self.assertIn("background-color: rgb(255, 0, 0)", html)   # 小标题红底
        self.assertIn('<section><span leaf="">正文<strong>粗</strong></span></section>',
                      html)
        self.assertTrue(html.endswith("</p>"))                    # 末尾样式声明
        # 别在开头加空 section(抓包里那个 <br> 是人手打的)
        self.assertFalse(html.startswith("<section><br"))

    def test_always_draft(self):
        self.api.save_draft = AsyncMock(return_value="1001")
        self.api.crop_cover = AsyncMock(return_value={})
        r = run(self.api.publish_article(ArticleDraft(title="t", markdown="正文"),
                                         publish=True))    # 即便传 True
        self.assertTrue(r.is_draft)                        # 也只能到草稿
        self.assertEqual(r.article_id, "1001")

    def test_draft_fields_indexed_per_article(self):
        """多图文靠字段名的数字后缀区分,索引错了就会串条。"""
        captured = {}

        async def fake_post(url, data=None, **kw):
            body = data.decode() if isinstance(data, bytes) else str(data)
            captured["body"] = body

            class R:
                status_code = 200
                text = json.dumps({"base_resp": {"ret": 0}, "appMsgId": "77"})
            return R()

        self.api.post = fake_post
        appmsgid = run(self.api.save_draft([
            {"title": "甲", "content": "<p>1</p>", "cover": "c1", "crops": {}},
            {"title": "乙", "content": "<p>2</p>", "cover": "c2", "crops": {}},
        ]))
        self.assertEqual(appmsgid, "77")
        body = captured["body"]
        self.assertIn('name="title0"', body)
        self.assertIn('name="title1"', body)
        self.assertIn('name="count"', body)
        # 摘要留空必须两个字段一起,否则微信自动补摘要
        self.assertIn('name="auto_gen_digest0"', body)


class ArticleSessionFactoryTests(unittest.TestCase):
    """registry.article_session 工厂:app 层与引擎体检共用的唯一入口。"""

    def test_factory_maps_platforms(self):
        from application.registry import article_session
        cls, kw = article_session("baijiahao", "a=1; b=2")
        self.assertIs(cls, BaijiahaoSession)
        self.assertEqual(kw, {"cookie": "a=1; b=2"})
        cls, kw = article_session("toutiao", "sid=x")
        self.assertIs(cls, ToutiaoSession)
        cls, kw = article_session("wechat_mp", "slave_sid=y||123456")
        self.assertIs(cls, WechatMpSession)
        self.assertEqual(kw, {"cookie": "slave_sid=y", "token": "123456"})


class RollingCookieTests(unittest.TestCase):
    """Cookie 滚动续期:凭证形态与合并语义。"""

    def test_wechat_stored_credentials_keeps_token(self):
        s = WechatMpSession(cookie="slave_sid=y", token="123456")
        self.assertEqual(s.stored_credentials("slave_sid=z; new=1"),
                         "slave_sid=z; new=1||123456")

    def test_default_stored_credentials_passthrough(self):
        s = BaijiahaoSession(cookie="BDUSS=x")
        self.assertEqual(s.stored_credentials("BDUSS=y"), "BDUSS=y")

    def test_merge_overwrites_same_name_keeps_rest(self):
        base = ArticlePlatformSession.parse_cookie("a=1; b=2; c=3")
        updated = {**base, **{"b": "9", "d": "4"}}
        self.assertEqual(updated, {"a": "1", "b": "9", "c": "3", "d": "4"})


if __name__ == "__main__":
    unittest.main()
