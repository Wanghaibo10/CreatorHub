"""抖音纯协议发布:不打网络的结构单测。"""
from __future__ import annotations

import json

from application.douyin.api import (
    CREATOR_AID, IMAGEX_APP_ID, VISIBILITY, _multipart, _sigv4,
    browser_query, build_create_v2, cookies_from_state, new_creation_id,
    normalize_ua, parse_ware_csrf)


def test_cookies_from_state_只收抖音域():
    state = json.dumps({
        "cookies": [
            {"name": "sessionid", "value": "s1", "domain": ".douyin.com"},
            {"name": "sid_tt", "value": "t1", "domain": "www.douyin.com"},
            {"name": "other", "value": "x", "domain": ".kuaishou.com"},
        ]
    })
    ck = cookies_from_state(state)
    assert ck["sessionid"] == "s1"
    assert ck["sid_tt"] == "t1"
    assert "other" not in ck


def test_空state给空dict():
    assert cookies_from_state("") == {}
    assert cookies_from_state("{") == {}


def test_可见性枚举():
    assert VISIBILITY["public"] == 0
    assert VISIBILITY["private"] == 1
    assert VISIBILITY["friends"] == 2
    assert CREATOR_AID == "1128"
    assert IMAGEX_APP_ID == "2906"


def test_create_v2_包形对得上实发抓包():
    cid = new_creation_id(1786974189281)
    assert cid.endswith("1786974189281")
    assert len(cid) == 8 + 13
    payload = build_create_v2(
        "v1e00fgi0000da1gvrvog65utlehjp20", "协议探测2", "第二次，等到真成功",
        visibility="private", allow_save=False, creation_id=cid)
    item = payload["item"]
    common = item["common"]
    assert set(item) == {
        "common", "cover", "mix", "selected_member", "chapter",
        "anchor", "sync", "open_platform", "assistant",
    }
    assert common["item_title"] == "协议探测2"
    assert common["caption"] == "第二次，等到真成功"
    assert common["text"] == "协议探测2 第二次，等到真成功"
    assert common["visibility_type"] == 1
    assert common["download"] == 0
    assert common["media_type"] == 4
    assert common["video_id"] == "v1e00fgi0000da1gvrvog65utlehjp20"
    assert common["creation_id"] == cid
    assert common["music_id"] is None
    assert "poster" not in item["cover"]
    pub = build_create_v2(
        "v0d00fg10000da1hfqvog65n02bljnmg", "巧克力小笼包让我秒懂菠萝披萨",
        "换我也破防，意大利人看了都得点头。直到我刷到巧克力小笼包。",
        visibility="public", allow_save=True,
        poster="tos-cn-i-jm8ajry58r/10d93cc5dbcc4e0ab8e7ecb61f81ca44")
    assert pub["item"]["common"]["visibility_type"] == 0
    assert pub["item"]["common"]["download"] == 1
    assert pub["item"]["cover"]["poster"].startswith("tos-cn-i-jm8ajry58r/")


def test_multipart_顺序与边界():
    body, ct = _multipart([
        ("video_id", "vid1"),
        ("item_title", "标题"),
        ("poster", ("p.jpg", b"\xff\xd8", "image/jpeg")),
    ])
    assert ct.startswith("multipart/form-data; boundary=")
    assert b'name="video_id"' in body
    assert b"vid1" in body
    assert body.find(b"video_id") < body.find(b"item_title")
    assert body.endswith(b"--\r\n")


def test_浏览器抓包过滤创作域():
    from application.douyin.publish import _SNIFF_HOST, _redact

    assert _SNIFF_HOST.search("https://creator.douyin.com/web/api/media/upload/auth/v5/")
    assert _SNIFF_HOST.search("https://vod.bytedanceapi.com/top/v1?Action=ApplyUploadInner")
    assert not _SNIFF_HOST.search("https://www.douyin.com/aweme/v1/web/aweme/detail")
    assert "***" in _redact("Cookie: sessionid=abc123")


def test_browser_query_跟create_v2抓包():
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/151.0.0.0 Safari/537.36")
    q = browser_query(ua)
    assert q["browser_name"] == "Mozilla"
    assert q["browser_version"].startswith("5.0 (Macintosh")
    assert "Chrome/151.0.0.0" in q["browser_version"]
    assert q["browser_platform"] == "MacIntel"
    assert q["support_h265"] == "1"
    win = browser_query("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0")
    assert win["browser_platform"] == "Win32"


def test_parse_ware_csrf_只要中间那段():
    assert parse_ware_csrf("0,tok123,sign456") == "tok123"
    assert parse_ware_csrf("") == ""
    assert parse_ware_csrf("onlyone") == ""


def test_create_v2_query_跟浏览器指纹_且不用passport冒充csrf():
    from application.douyin.api import DouyinAPI
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/151.0.0.0 Safari/537.36")
    api = DouyinAPI({"sessionid": "x", "passport_csrf_token": "WRONG"}, ua=ua)
    assert api._csrf == ""
    url = api._signed_url("/web/api/media/aweme/create_v2/",
                          extra={"read_aid": IMAGEX_APP_ID})
    assert "aid=1128" in url
    assert "read_aid=2906" in url
    assert "browser_name=Mozilla" in url
    assert "browser_name=Chrome" not in url
    assert "131.0.0.0" not in url


def test_ingest_csrf_写进请求头():
    from application.douyin.api import DouyinAPI

    class _Resp:
        def __init__(self, headers):
            self.headers = headers

    api = DouyinAPI({"sessionid": "x", "passport_csrf_token": "WRONG"})
    api._ingest_csrf(_Resp({"x-ware-csrf-token": "0,abcToken,sig"}))
    assert api._csrf == "abcToken"
    assert api._creator_headers()["x-secsdk-csrf-token"] == "abcToken"
    assert "WRONG" not in api._creator_headers().get("x-secsdk-csrf-token", "")


def test_sigv4_头齐全():
    sts = {
        "access_key_id": "AKID",
        "secret_access_key": "SECRET",
        "session_token": "TOK",
    }
    h = _sigv4("GET", "/top/v1", {"Action": "ApplyUploadInner", "SpaceName": "aweme"},
               sts)
    assert h["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "sdwdmwlll/vod/aws4_request" in h["authorization"]
    assert h["x-amz-security-token"] == "TOK"
    assert "x-amz-date" in h


def test_UA_里的HeadlessChrome必须洗掉():
    """库里 acc.ua 是 patchright 原始串,写着 HeadlessChrome —— 它会同时进
    User-Agent 头和风控 query 的 browser_version,等于自报两次无头浏览器。"""
    raw = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36")
    ua = normalize_ua(raw)
    assert "Headless" not in ua
    assert "Chrome/" in ua
    # query 是从 UA 派生的,也不许漏
    assert "Headless" not in browser_query(ua)["browser_version"]


def test_UA版本与impersonate目标一致():
    """真 Chrome 的 UA 与 Sec-Ch-Ua 永远同版本;impersonate 只有有限几个目标,
    所以要把 UA 的大版本拉到目标版本上,免得 UA 说 151、Sec-Ch-Ua 说 146。"""
    import re
    from moss.common.netfp import impersonate_for_ua
    for raw in ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/151.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HeadlessChrome/130.0.0.0 Safari/537.36"):
        ua = normalize_ua(raw)
        major = re.search(r"Chrome/(\d+)", ua).group(1)
        assert impersonate_for_ua(ua) == "chrome" + major, (raw, ua)


def test_创作域请求头是XHR形不是导航形():
    """curl_cffi impersonate 默认头是顶层导航形;带 Origin 的 POST 配那套
    = 跨站表单指纹,正是 CSRF 防护要拦的。抓包里 create_v2 的 resource 是 xhr。"""
    from application.douyin.api import DouyinAPI
    api = DouyinAPI({"sessionid": "x"})
    h = api._creator_headers(content_type="application/json")
    assert h["Sec-Fetch-Dest"] == "empty"
    assert h["Sec-Fetch-Mode"] == "cors"
    assert h["Sec-Fetch-Site"] == "same-origin"
    # None = 让 curl_cffi 删掉 impersonate 自带的那两个导航头
    assert h["Sec-Fetch-User"] is None
    assert h["Upgrade-Insecure-Requests"] is None
    # Accept-Language 必须跟 query 的 browser_language 一致,不能是 en-US
    assert h["Accept-Language"].startswith("zh-CN")
    assert browser_query(api.ua)["browser_language"] == "zh-CN"

def test_creator头对齐真实浏览器():
    """逐项对齐 2026-08-18 CDP 抓到的完整真实头(见上一条测试的出处)。"""
    from application.douyin.api import DouyinAPI
    api = DouyinAPI({"sessionid": "x"}, "Mozilla/5.0 Chrome/142.0.0.0", "")
    h = {k.lower(): v for k, v in api._creator_headers().items()}
    assert h["sec-fetch-dest"] == "empty"
    assert h["sec-fetch-mode"] == "cors"
    assert h["sec-fetch-site"] == "same-origin"
    assert h["priority"] == "u=1, i"
    assert h["accept-language"].startswith("zh-CN")
    # 真实头里没有这两个,值给 None 让 curl_cffi 删掉默认头
    assert h.get("sec-fetch-user") is None
    assert h.get("upgrade-insecure-requests") is None


def test_写操作带ticket_guard签名头():
    """创作者域**写操作**必须带 bd-ticket-guard-* 五件套。

    2026-08-18 实证:不带 → 403 空 body,判为异常客户端,**同时烧掉登录态**
    (DB 快照与浏览器会话一起失效);带上 → 200 + 要求身份验证,与真实浏览器
    首次发布同形。算法见 signing/ticket_guard.py。
    """
    from application.douyin.api import DouyinAPI
    api = DouyinAPI({"sessionid": "x"}, "Mozilla/5.0 Chrome/142.0.0.0", "",
                    storage_state=_fake_state())
    h = {k.lower(): v for k, v in
         api._creator_headers(content_type="application/json",
                              sign_path="/web/api/media/aweme/create_v2/").items()}
    assert h["bd-ticket-guard-version"] == "2"
    assert h["bd-ticket-guard-web-sign-type"] == "1"
    assert h["bd-ticket-guard-web-version"] == "2"
    assert h["bd-ticket-guard-ree-public-key"]
    import base64, json as _j
    cd = _j.loads(base64.b64decode(h["bd-ticket-guard-client-data"]))
    assert cd["req_content"] == "ticket,path,timestamp"


def test_读操作不带签名头():
    """签名覆盖 path,读接口不需要;省一次 ECDH。"""
    from application.douyin.api import DouyinAPI
    api = DouyinAPI({"sessionid": "x"}, "Mozilla/5.0 Chrome/142.0.0.0", "",
                    storage_state=_fake_state())
    h = {k.lower() for k in api._creator_headers()}
    assert not any(k.startswith("bd-ticket-guard") for k in h)


def test_没有签名材料时写操作直接失败():
    """**不许退化成不带签名发出去** —— 那等于拿登录态去换一个 403。"""
    import pytest as _pt
    from application.douyin.api import DouyinAPI
    from application.douyin.signing.ticket_guard import TicketGuardUnavailable
    api = DouyinAPI({"sessionid": "x"}, "Mozilla/5.0 Chrome/142.0.0.0", "")
    with _pt.raises(TicketGuardUnavailable):
        api._creator_headers(sign_path="/web/api/media/aweme/create_v2/")


def _fake_state():
    """一副当场生成的密钥,不是任何真实账号的。"""
    import datetime, json as _j
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    srv = ec.generate_private_key(ec.SECP256R1())
    nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    cert = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm)
            .public_key(srv.public_key()).serial_number(1)
            .not_valid_before(datetime.datetime(2020, 1, 1))
            .not_valid_after(datetime.datetime(2030, 1, 1))
            .sign(srv, hashes.SHA256()))
    pem = priv.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    return {"cookies": [], "origins": [{
        "origin": "https://creator.douyin.com", "localStorage": [
            {"name": "security-sdk/s_sdk_crypt_sdk",
             "value": _j.dumps({"data": _j.dumps({"ec_privateKey": pem})})},
            {"name": "security-sdk/s_sdk_server_cert_key",
             "value": _j.dumps({"cert": cert.public_bytes(
                 serialization.Encoding.PEM).decode()})},
            {"name": "security-sdk/s_sdk_cert_key",
             "value": _j.dumps({"data": "pub.AAAA"})},
            {"name": "security-sdk/s_sdk_sign_data_key/web_protect",
             "value": _j.dumps({"data": _j.dumps(
                 {"ticket": "hash.TT", "ts_sign": "ts.2.SS"})})}]}]}


def test_空body带verify头时报出身份验证():
    """`200 + 空 body + x-tt-verify-passport-decision` = **要求本人身份验证**,
    不是 bug。2026-08-18 实证:真实浏览器首次发布也是这个响应,人过一次短信
    验证码(send_code → validate_code)后重发即成功。

    ⚠️ 错误信息必须点名「身份验证」——否则上层只看到「HTTP 200 空 body」,
    会当成协议坏了,然后去改签名、改指纹,把时间全花在错的地方(本轮实录)。
    """
    from application.douyin.api import verify_required
    assert verify_required({"x-tt-verify-passport-decision":
                            '{"account_flow":"verify"}'})
    assert verify_required({"X-Tt-Verify-Passport-Decision":
                            '{"account_flow":"verify"}'})
    assert not verify_required({"content-type": "application/json"})
    assert not verify_required({})
