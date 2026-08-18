"""抖音「本人身份验证」—— 创作者域写操作被风控要求验证时走这条。

2026-08-18 从真实浏览器发布抓包逆出(1297 条网络记录)。触发形态::

    POST create_v2 → HTTP 200 + **空 body** + x-tt-verify-passport-decision
                     {"account_flow":"verify", "encrypt_uid":"…",
                      "verify_way_name_list":"mobile_sms_verify,qr_code_verify"}

完整流程(抓包实录,两次发布都一样)::

    +0.0s   POST create_v2               → 200 空 body,要求验证
    +27.1s  POST passport/web/send_code/    → {"mobile":"156********","retry_time":60}
    +39.1s  POST passport/web/validate_code/ → {"ticket":"VTIDEF…"}
    +39.6s  POST create_v2               → 200 + item_id ✅

⚠️ 三条实证:

1. **`code` 要 hex 编码**:抓包里是 `code=353531303433`,那是
   `hex(ascii("551043"))`,不是 12 位验证码。直填 6 位会判错,而错误次数有限。
2. **`encrypt_uid` 就在 create_v2 的响应头里**,不必再调 `query_decision`。
3. **这三个接口不需要 bd-ticket-guard 签名**(抓包实证),只要 csrf ——
   与 create_v2 不同,别照抄那套头。

⚠️ 验证要求来自**账号风控状态**,不是协议缺陷:同一天真实浏览器手动发布
两次,两次都被要求验证。所以它不是"我们的客户端有问题"的证据。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

SEND_PATH = "/passport/web/send_code/"
VALIDATE_PATH = "/passport/web/validate_code/"

#: 创作者场景的固定表单值,逐字段对 2026-08-18 抓包
_TYPE = "3737"
_AID = "2906"
_SDK = "1.0.0.420-web"
WAY_SMS = "mobile_sms_verify"

#: 这三个接口的公共 query(抓包原样)
QUERY = {
    "passport_jssdk_version": "5.1.2", "passport_jssdk_type": "lite",
    "is_from_ttaccountsdk": "1", "aid": _AID, "language": "zh",
    "account_app_language": "zh-CN", "new_authn_sdk_version": _SDK,
    "is_vcd": "1",
}


def encode_code(code: str) -> str:
    """6 位验证码 → hex。见模块 docstring 坑 1。"""
    return "".join(f"{ord(c):02x}" for c in str(code).strip())


def decision_from_headers(headers: Any) -> Optional[Dict[str, Any]]:
    """响应头 → 验证要求。没有/解析不出返回 None(**不抛**,别炸发布流程)。"""
    raw = ""
    for k, v in (headers or {}).items():
        if k.lower() == "x-tt-verify-passport-decision" and v:
            raw = v
            break
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:                                            # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    ev = d.get("event_params") or {}
    return {
        "encrypt_uid": d.get("encrypt_uid") or "",
        "ways": d.get("verify_way_name_list") or "",
        "scene": ev.get("verify_scene") or "creator",
        "reason": ev.get("verify_reason") or "",
        "raw": d,
    }


def code_form(encrypt_uid: str, *, scene: str = "creator",
              code: str = "", way: str = WAY_SMS) -> Dict[str, str]:
    """send_code / validate_code 的表单。给了 code 就是 validate。

    ⚠️ 字段逐个对抓包 —— 少一个服务端就不认;`is6Digits` 只在 send 里有。
    """
    f = {"mix_mode": "1", "type": _TYPE, "encrypt_uid": encrypt_uid,
         "copywriting_key": scene, "aid": _AID,
         "std_verify_way": way, "new_authn_sdk_version": _SDK}
    if code:
        f["code"] = encode_code(code)
    else:
        f["is6Digits"] = "1"
    return f
