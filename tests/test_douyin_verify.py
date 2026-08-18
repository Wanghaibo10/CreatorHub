"""抖音「本人身份验证」流程的结构单测(不打网络)。

2026-08-18 从真实浏览器发布抓包逆出。触发条件:创作者域写操作返回
`200 + 空 body + x-tt-verify-passport-decision`(见 api.verify_required)。
流程:decision → send_code(发短信) → validate_code(校验) → 重发原请求。

⚠️ 这三个接口**不需要 bd-ticket-guard 签名**(抓包实证),只要 csrf。
"""
import json

import pytest

from application.douyin import verify as V


def test_验证码要按hex编码():
    """抓包里 `code=353531303433` 不是 12 位验证码,是 hex(ascii("551043"))。

    ⚠️ 直接把 6 位数字填进去会被判验证码错误 —— 而错误次数是有限的。
    """
    assert V.encode_code("551043") == "353531303433"
    assert V.encode_code("000000") == "303030303030"


def test_从响应头解出decision():
    """`encrypt_uid` 直接在 create_v2 的响应头里,不必再调 query_decision。"""
    hdr = {"x-tt-verify-passport-decision": json.dumps({
        "account_flow": "verify", "encrypt_uid": "UID123",
        "verify_way_name_list": "mobile_sms_verify,qr_code_verify",
        "event_params": {"verify_scene": "creator"}})}
    d = V.decision_from_headers(hdr)
    assert d and d["encrypt_uid"] == "UID123"
    assert "mobile_sms_verify" in d["ways"]
    assert d["scene"] == "creator"


def test_大小写不敏感():
    hdr = {"X-Tt-Verify-Passport-Decision": '{"account_flow":"verify","encrypt_uid":"U"}'}
    assert V.decision_from_headers(hdr)["encrypt_uid"] == "U"


def test_没有decision返回None():
    assert V.decision_from_headers({}) is None
    assert V.decision_from_headers({"content-type": "application/json"}) is None


def test_decision解析不出不许抛():
    """响应头是外部输入,坏了也不能把发布流程炸掉。"""
    assert V.decision_from_headers({"x-tt-verify-passport-decision": "不是json"}) is None


def test_表单字段与抓包一致():
    """逐字段对 2026-08-18 抓包。少一个字段服务端就不认。"""
    f = V.code_form("UID123", scene="creator")
    assert f["mix_mode"] == "1" and f["type"] == "3737"
    assert f["encrypt_uid"] == "UID123"
    assert f["std_verify_way"] == "mobile_sms_verify"
    assert f["copywriting_key"] == "creator"
    assert f["aid"] == "2906"
    assert f["is6Digits"] == "1"
    assert "code" not in f                      # send 不带 code
    g = V.code_form("UID123", scene="creator", code="551043")
    assert g["code"] == "353531303433"          # validate 带 hex 后的
    assert "is6Digits" not in g                 # 抓包里 validate 没这个字段


@pytest.mark.asyncio
async def test_api_发验证码走form不带签名头(monkeypatch):
    """passport 接口是 form 编码、**不带 ticket-guard**(抓包实证)。
    若照抄 create_v2 那套头,等于给一个不需要签名的接口发签名 —— 徒增指纹差异。
    """
    from application.douyin.api import DouyinAPI
    api = DouyinAPI({"sessionid": "x"}, "Mozilla/5.0 Chrome/142.0.0.0", "")
    seen = {}

    class FakeResp:
        status_code = 200
        content = b'{"data":{"mobile":"156****","retry_time":60},"message":"success"}'
        headers = {}

        def json(self):
            return json.loads(self.content)

    class FakeCli:
        async def request(self, method, url, headers=None, data=None, **kw):
            seen.update(method=method, url=url, headers=headers or {}, data=data)
            return FakeResp()

    api._cli = FakeCli()
    out = await api.send_verify_code("UID123")
    assert out["mobile"] == "156****" and out["retry_time"] == 60
    assert seen["method"] == "POST"
    assert V.SEND_PATH in seen["url"]
    hl = {k.lower() for k in seen["headers"]}
    assert not any(k.startswith("bd-ticket-guard") for k in hl)
    assert seen["headers"].get("Content-Type") == "application/x-www-form-urlencoded"
    assert b"encrypt_uid=UID123" in seen["data"]
    assert b"is6Digits=1" in seen["data"]


@pytest.mark.asyncio
async def test_api_校验验证码返回ticket(monkeypatch):
    from application.douyin.api import DouyinAPI
    api = DouyinAPI({"sessionid": "x"}, "Mozilla/5.0 Chrome/142.0.0.0", "")
    seen = {}

    class FakeResp:
        status_code = 200
        content = b'{"data":{"ticket":"VTICKET"},"message":"success"}'
        headers = {}

        def json(self):
            return json.loads(self.content)

    class FakeCli:
        async def request(self, method, url, headers=None, data=None, **kw):
            seen.update(data=data, url=url)
            return FakeResp()

    api._cli = FakeCli()
    assert await api.validate_verify_code("UID123", "551043") == "VTICKET"
    assert b"code=353531303433" in seen["data"]      # hex 编码过的
    assert V.VALIDATE_PATH in seen["url"]
