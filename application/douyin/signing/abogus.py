"""纯 Python a_bogus 生成器。

对应逆向符号:
  signing.(*ABogus).GetValue / generateString1 / generateString2 / generateString2List
  signing.(*ABogus).generateMethodCode / generateParamsCode / generateResult / randomList

⚠️ 这是抖音 Web 风控签名,算法跟随抖音前端 JS(acrawler/acsl)变化。
   若 list 接口返回空/验证码,多半是抖音换算法了 —— 需要从最新 JS dump 校正
   下面 ABogus.ALPHABET / FINGERPRINT / REG 等常量。结构保持与原 JS 一致,方便比对。

用法:
  ab = ABogus(user_agent=UA)
  a_bogus = ab.get_value(query_string, body="")
  # 把 a_bogus 追加到 URL query 即可
"""
from __future__ import annotations

import random
import time
from typing import List

from application.douyin.signing.sm3 import sm3_to_array
from application.douyin.signing.rc4 import rc4

# ─── 可调常量(抖音换算法时改这里)───────────────────────────────
# 自定义 base64 字母表(注意非标准顺序)
ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
# 浏览器指纹常量(取自 acrawler 对 canvas/window 的探测结果)
REG = [1937774191, 1226093241, 388252375, 3666478592,
       2842636476, 372324522, 3817729613, 2969243214]
# 版本/参数标记
ARGUMENTS = [0, 1, 14]
# ────────────────────────────────────────────────────────────────


def _list_to_str(lst: List[int]) -> str:
    return "".join(chr(c & 0xFF) for c in lst)


def _rc4_str(key: str, data: str) -> str:
    return _list_to_str(rc4(key.encode("latin1"), data.encode("latin1")))


class ABogus:
    def __init__(self, user_agent: str = "Mozilla/5.0", fp: str = ""):
        self.ua = user_agent
        self.fp = fp or self._default_fp()

    # ── 浏览器指纹(伪造 window.* 探测值)──
    def _default_fp(self) -> str:
        return (
            "1536|747|1536|834|0|30|0|0|1536|834|1536|834|1525|747|24|24|"
            "Win32"
        )

    # ── randomList:用时间戳+随机数构造扰动列表 ──
    def _random_list(self, a=170, b=85, c=45, d=0, e=0, f=0, g=0):
        r = random.random() * 10000
        v = [r, int(r) & 255, int(r) >> 8 & 255]
        s = v[1] & a | d
        v.append(v[1] & b | e)
        s2 = v[1] & c | f
        v.append(v[2] & a | g)
        return [s, v[3], s2, v[4]]

    # ── 自定义 base64(用 ALPHABET) ──
    @staticmethod
    def _b64(data: List[int]) -> str:
        out = ""
        i = 0
        n = len(data)
        while i < n:
            b0 = data[i] & 0xFF
            b1 = data[i + 1] & 0xFF if i + 1 < n else 0
            b2 = data[i + 2] & 0xFF if i + 2 < n else 0
            out += ALPHABET[b0 >> 2]
            out += ALPHABET[((b0 & 3) << 4) | (b1 >> 4)]
            if i + 1 < n:
                out += ALPHABET[((b1 & 15) << 2) | (b2 >> 6)]
            else:
                out += "="
            if i + 2 < n:
                out += ALPHABET[b2 & 63]
            else:
                out += "="
            i += 3
        return out

    # ── generateString2List:对 query/body 取 SM3 摘要 ──
    def _digest(self, params: str, body: str) -> List[int]:
        p = sm3_to_array(sm3_to_array(params + self.fp))
        b = sm3_to_array(sm3_to_array(body))
        ua = sm3_to_array(_rc4_str(_list_to_str(ARGUMENTS), self.ua))
        return p, b, ua

    # ── generateResult:组装最终字节列表 ──
    def get_value(self, params: str, body: str = "") -> str:
        now_ms = int(time.time() * 1000)
        p_hash, b_hash, ua_hash = self._digest(params, body)
        rnd = self._random_list()

        buf: List[int] = []
        buf += ARGUMENTS
        buf += rnd
        # 时间戳(大端 4 字节)
        buf += [(now_ms >> (8 * i)) & 0xFF for i in (3, 2, 1, 0)]
        # 三段摘要各取前 6 字节(与原 JS 截断一致)
        buf += p_hash[:6]
        buf += b_hash[:6]
        buf += ua_hash[:6]
        # 浏览器指纹常量
        for r in REG:
            buf += [(r >> (8 * i)) & 0xFF for i in (3, 2, 1, 0)]

        # 校验字节(异或和)
        check = 0
        for x in buf:
            check ^= x & 0xFF
        buf.append(check & 0xFF)

        # RC4 + 自定义 base64
        salt = str(now_ms)
        enc = rc4(salt.encode("latin1"), buf)
        return self._b64(enc)


def sign_url(query_string: str, user_agent: str, body: str = "") -> str:
    """便捷函数:返回追加了 a_bogus 的完整 query string。"""
    ab = ABogus(user_agent=user_agent)
    a_bogus = ab.get_value(query_string, body)
    sep = "&" if query_string else ""
    return f"{query_string}{sep}a_bogus={a_bogus}"
