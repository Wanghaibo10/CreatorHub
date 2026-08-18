"""bd-ticket-guard 客户端签名的结构单测(不打网络、不含真实密钥)。

⚠️ 真实性验证不在这里,而是 2026-08-18 的实证:
两组真实抓包样本(timestamp 1787031853 / 1787032516,同 ticket 同 path)
用本模块复算 `req_sign` **逐字节一致**;随后用实时签名(距发出 1 秒)
打 create_v2,服务端从 403(异常客户端)转为 200+要求身份验证 ——
与真实浏览器首次发布同形。算法出处见模块 docstring。

这里只锁**结构**:明文格式、确定性、client_data 形状、材料提取。
测试密钥是当场生成的,不是账号的。
"""
import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from application.douyin.signing import ticket_guard as tg


@pytest.fixture
def keypair():
    """临时密钥对 + 自签证书,只为跑通 ECDH,不涉及任何真实账号。"""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    import datetime

    priv = ec.generate_private_key(ec.SECP256R1())
    srv = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(srv.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime(2020, 1, 1))
            .not_valid_after(datetime.datetime(2030, 1, 1))
            .sign(srv, hashes.SHA256()))
    return (priv.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()).decode(),
            cert.public_bytes(serialization.Encoding.PEM).decode())


def test_签名明文是url_query格式():
    """SDK 原文: i = `ticket=${a}&path=${c}&timestamp=${e}`

    ⚠️ 这一行是整个逆向的关键。此前盲试了 7 种拼接(逗号/换行/竖线/JSON…)
    全部不中,读源码一眼就看见。**别再改成"看起来更合理"的拼法。**
    """
    assert tg.sign_plain("TK", "/a/b/", 123) == "ticket=TK&path=/a/b/&timestamp=123"


def test_同输入签名确定(keypair):
    priv, cert = keypair
    key = tg.derive_key(priv, cert)
    a, _ = tg.sign(key, "TK", "/p/", 1787031853)
    b, _ = tg.sign(key, "TK", "/p/", 1787031853)
    assert a == b
    assert len(base64.b64decode(a)) == 32          # HMAC-SHA256,不是 ECDSA 的 64


def test_timestamp不同则签名不同(keypair):
    priv, cert = keypair
    key = tg.derive_key(priv, cert)
    assert tg.sign(key, "TK", "/p/", 1)[0] != tg.sign(key, "TK", "/p/", 2)[0]


def test_path不同则签名不同(keypair):
    """签名覆盖 path ⇒ **不能跨接口复用**同一份 client-data。"""
    priv, cert = keypair
    key = tg.derive_key(priv, cert)
    assert tg.sign(key, "TK", "/a/", 1)[0] != tg.sign(key, "TK", "/b/", 1)[0]


def test_client_data是base64的json(keypair):
    priv, cert = keypair
    key = tg.derive_key(priv, cert)
    cd = tg.client_data(key, "TK", "ts.2.xx", "/p/", 1787031853)
    d = json.loads(base64.b64decode(cd))
    assert d["req_content"] == "ticket,path,timestamp"
    assert d["ts_sign"] == "ts.2.xx"
    assert d["timestamp"] == 1787031853
    assert len(base64.b64decode(d["req_sign"])) == 32


def test_从storage_state提取材料():
    """材料在 `creator_storage_state` 的 origins[creator.douyin.com].localStorage
    里就有(2026-08-18 实证 4 个 security-sdk/* 键),不必另存一份、不必改表。"""
    state = {"cookies": [], "origins": [
        {"origin": "https://lf-zt.douyin.com",
         "localStorage": [{"name": "security-sdk/s_sdk_cert_key", "value": "别用这个"}]},
        {"origin": "https://creator.douyin.com", "localStorage": [
            {"name": "security-sdk/s_sdk_cert_key", "value": '{"data":"pub.AAA"}'},
            {"name": "security-sdk/s_sdk_crypt_sdk", "value": "x"},
            {"name": "其他", "value": "y"}]}]}
    got = tg.store_from_state(state)
    assert got["security-sdk/s_sdk_cert_key"] == '{"data":"pub.AAA"}'
    assert "其他" not in got


def test_材料缺失时明确报错():
    """没有 security-sdk 材料就**不要静默发一个没签名的请求** ——
    那会被判成异常客户端并烧掉登录态(2026-08-18 实测 403 即作废会话)。"""
    with pytest.raises(tg.TicketGuardUnavailable):
        tg.headers_from_state({"cookies": [], "origins": []},
                              "/web/api/media/aweme/create_v2/")
