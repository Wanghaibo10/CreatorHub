"""bd-ticket-guard 客户端签名 —— 抖音创作者域**写操作**的准入凭证。

2026-08-18 从 `auth.zijieapi.com/ucenter_web/zero/version/dist/latest/
index.umd.production.js` 逆出。SDK 原文(压缩后)::

    let e = Math.floor(new Date().getTime()/1e3),
        i = `ticket=${a}&path=${c}&timestamp=${e}`,
        o = new th({privateKey:n, publicKey:r});
    if (p && g && l) try { u = (yield o.signWithHmac(i, l)).result; w="hmac" }
    catch(e) { w = "ecdsa" }
    let h = {ts_sign:s, req_content:"ticket,path,timestamp", req_sign:u, timestamp:e};

算法::

    ecdh   = ECDH(ec_privateKey, 服务端证书公钥)          # 都是 secp256r1
    key    = HKDF-SHA256(ecdh, length=32)                # SDK 里的 ecdhKey
    plain  = f"ticket={ticket}&path={path}&timestamp={ts}"
    req_sign = base64(HMAC-SHA256(key, plain))

实证(2026-08-18)::

    两组真实抓包样本(ts=1787031853 / 1787032516,同 ticket 同 path)
    复算 req_sign **逐字节一致**;实时签名(距发出 1 秒)打 create_v2,
    服务端从 **403 空 body**(判为异常客户端,并当场作废整个账号会话)
    转为 **200 + 要求身份验证** —— 与真实浏览器首次发布同形。

⚠️ 三条踩过的坑:

1. **明文是 URL query 格式**。此前盲试逗号/换行/竖线/JSON 等 7 种拼接全不中,
   读源码一眼就有。别改成"看起来更合理"的拼法。
2. **签名覆盖 path** ⇒ 不能跨接口复用同一份 client-data;timestamp 也在里面,
   有时效,不能缓存太久。
3. **没有材料时必须报错,不能静默发无签名请求** —— 那会被判异常客户端,
   403 的同时把登录态一起烧掉(DB 快照与浏览器会话同时失效,实测 6 次)。

材料来源:`creator_storage_state` 的 origins[creator.douyin.com].localStorage
里就有(登录时 playwright 一并存下),不必改表、不必另抓。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: 签名材料在 localStorage 里的键,全部以此开头
PREFIX = "security-sdk/"
ORIGIN = "https://creator.douyin.com"

K_PRIV = "security-sdk/s_sdk_crypt_sdk"
K_CERT = "security-sdk/s_sdk_server_cert_key"
K_PUB = "security-sdk/s_sdk_cert_key"
K_SIGN = "security-sdk/s_sdk_sign_data_key/web_protect"


class TicketGuardUnavailable(RuntimeError):
    """缺签名材料。**调用方必须让请求失败,不要退化成不带签名发出去。**"""


def sign_plain(ticket: str, path: str, ts: int) -> str:
    """签名明文。见模块 docstring 的坑 1 —— 这个格式是从源码抄的,不要改。"""
    return f"ticket={ticket}&path={path}&timestamp={ts}"


def derive_key(ec_private_pem: str, server_cert_pem: str) -> bytes:
    """ECDH(设备私钥, 服务端证书公钥) → HKDF-SHA256 → 32 字节 HMAC key。"""
    priv = serialization.load_pem_private_key(ec_private_pem.encode(), password=None)
    pub = x509.load_pem_x509_certificate(server_cert_pem.encode()).public_key()
    shared = priv.exchange(ec.ECDH(), pub)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=None).derive(shared)


def sign(key: bytes, ticket: str, path: str, ts: int | None = None) -> tuple[str, int]:
    """→ (req_sign, timestamp)。ts 省略则取当前秒。"""
    ts = int(ts if ts is not None else time.time())
    mac = hmac.new(key, sign_plain(ticket, path, ts).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode(), ts


def client_data(key: bytes, ticket: str, ts_sign: str, path: str,
                ts: int | None = None) -> str:
    """→ `bd-ticket-guard-client-data` 头的值(base64 的 JSON)。"""
    req_sign, ts = sign(key, ticket, path, ts)
    payload = {"ts_sign": ts_sign, "req_content": "ticket,path,timestamp",
               "req_sign": req_sign, "timestamp": ts}
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode()


def store_from_state(storage_state: Any) -> Dict[str, str]:
    """从 storage_state 抠出 creator 域的 security-sdk 材料。

    ⚠️ 只认 `creator.douyin.com` 那个 origin —— `lf-zt.douyin.com` 等
    静态域下也有同名键,但那是另一套密钥,拿错了签出来的名字对不上。
    """
    if isinstance(storage_state, str):
        try:
            storage_state = json.loads(storage_state or "{}")
        except Exception:                                        # noqa: BLE001
            return {}
    for o in (storage_state or {}).get("origins") or []:
        if (o.get("origin") or "").rstrip("/") != ORIGIN:
            continue
        return {it["name"]: it.get("value", "")
                for it in (o.get("localStorage") or [])
                if str(it.get("name", "")).startswith(PREFIX)}
    return {}


def headers_from_store(store: Dict[str, str], path: str,
                       ts: int | None = None) -> Dict[str, str]:
    """材料 dict → 五个 bd-ticket-guard-* 请求头。"""
    missing = [k for k in (K_PRIV, K_CERT, K_PUB, K_SIGN) if not store.get(k)]
    if missing:
        raise TicketGuardUnavailable(
            "缺 ticket-guard 材料 " + ",".join(m.split("/")[-1] for m in missing)
            + " —— 该账号要重新做一次创作者登录(材料随 storage_state 落库)")
    try:
        sd = json.loads(json.loads(store[K_SIGN])["data"])
        crypt = json.loads(json.loads(store[K_PRIV])["data"])
        cert = json.loads(store[K_CERT])["cert"]
        pub = json.loads(store[K_PUB])["data"]
    except Exception as exc:                                     # noqa: BLE001
        raise TicketGuardUnavailable(f"ticket-guard 材料解析失败: {exc!r}") from exc
    key = derive_key(crypt["ec_privateKey"], cert)
    return {
        "bd-ticket-guard-client-data": client_data(
            key, sd["ticket"], sd["ts_sign"], path, ts),
        "bd-ticket-guard-ree-public-key": pub[4:] if pub.startswith("pub.") else pub,
        "bd-ticket-guard-version": "2",
        "bd-ticket-guard-web-sign-type": "1",
        "bd-ticket-guard-web-version": "2",
    }


def headers_from_state(storage_state: Any, path: str,
                       ts: int | None = None) -> Dict[str, str]:
    return headers_from_store(store_from_state(storage_state), path, ts)
