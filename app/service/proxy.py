"""代理探测与归属地服务。

从 main.py 抽出(2026-08-17 模块化):这些函数被代理池路由和账号路由共用,
放在服务层,两边都从这里导入,避免任何一方 import 另一个 router 造成环。
"""
from __future__ import annotations

from typing import Any, Dict

from moss.model import ProxyPool


def _mask_proxy(proxy: str) -> str:
    """脱敏展示代理(隐藏账号密码)。

    逐字保留自 main.py 的原实现:裸 host:port 会补上 http:// 再规范化输出,
    解析失败一律回 "***"(宁可不显示,也不能把带密码的原串漏出去)。
    """
    if not proxy:
        return ""
    try:
        from urllib.parse import urlparse
        u = urlparse(proxy if "://" in proxy else "http://" + proxy)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        auth = "***@" if u.username else ""
        return f"{u.scheme}://{auth}{host}{port}"
    except Exception:
        return "***"

def _parse_ipinfo(j: dict) -> dict:
    return {"ip": j.get("ip", ""), "country": j.get("country", ""),
            "region": j.get("region", ""), "city": j.get("city", ""),
            "isp": j.get("org", "")}


def _parse_ipapi(j: dict) -> dict:
    if j.get("status") != "success":
        return {}
    return {"ip": j.get("query", ""), "country": j.get("country", ""),
            "region": j.get("regionName", ""), "city": j.get("city", ""),
            "isp": j.get("isp", "")}


def _proxy_status_ok(status_code: int) -> bool:
    return _proxy_probe_status(status_code) == "ok"


def _proxy_probe_status(status_code: int) -> str:
    """Map a proxy probe response to a persisted health state."""
    if 200 <= status_code < 400:
        return "ok"
    if status_code == 407:
        return "auth_error"
    if status_code in {403, 429}:
        return "blocked"
    return "bad"

def _proxy_status_from_detail(ok: bool, detail: str) -> str:
    if ok:
        return "ok"
    text = str(detail or "")
    for code in (407, 403, 429):
        if f"HTTP {code}" in text:
            return _proxy_probe_status(code)
    return "bad"

async def _probe_proxy(url: str, platform: str = "douyin", timeout: float = 15):
    """经代理实连一次目标站,返回 (ok, detail)。"""
    import httpx
    if not url:
        return False, "未配置代理"
    test_url = ("https://www.xiaohongshu.com/" if platform == "xhs"
                else "https://www.kuaishou.com/" if platform == "kuaishou"
                else "https://channels.weixin.qq.com/" if platform == "shipinhao"
                else "https://www.douyin.com/")
    try:
        async with httpx.AsyncClient(proxy=url, timeout=timeout, follow_redirects=True) as cli:
            r = await cli.get(test_url)
        return _proxy_status_ok(r.status_code), f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

async def _proxy_geo(proxy_url: str, timeout: float = 8) -> dict | None:
    """经代理查出口 IP 及归属地(多源兜底)。返回 {ip,country,region,city,isp} 或 None。"""
    import httpx
    sources = [
        ("http://ip-api.com/json/?lang=zh-CN&fields=status,country,regionName,city,isp,query",
         _parse_ipapi),
        ("https://ipinfo.io/json", _parse_ipinfo),
    ]
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout,
                                     follow_redirects=True) as cli:
            for url, parser in sources:
                try:
                    g = parser((await cli.get(url)).json())
                    if g and g.get("ip"):
                        return g
                except Exception:
                    continue
    except Exception:
        pass
    return None

def _geo_text(g: dict | None) -> str:
    if not g:
        return ""
    loc = " · ".join([x for x in (g.get("country"), g.get("region"), g.get("city")) if x])
    parts = [p for p in (g.get("ip"), loc, g.get("isp")) if p]
    return "  ".join(parts)

async def _detect_proxy(raw: str) -> dict:
    """自动判别代理类型(HTTP / SOCKS5)与是否需要认证。
    对同一 host:port 依次试 [按输入协议 或 http+socks5] × [免密 / 带密(若输入含账密)],
    取第一个连通的组合。返回判别结果 + 推荐的规范化地址 + 浏览器兼容性。"""
    from urllib.parse import urlparse
    raw = (raw or "").strip()
    if not raw:
        return {"ok": False, "error": "请先填代理地址"}
    has_scheme = "://" in raw
    u = urlparse(raw if has_scheme else "http://" + raw)
    host, port = u.hostname, u.port
    if not host or not port:
        return {"ok": False, "error": "地址需含 host:port"}
    user, pwd = u.username, u.password
    cred = f"{user}:{pwd}@" if user else ""
    # 候选协议:输入已带则只测它,否则 http 与 socks5 都试
    if has_scheme and u.scheme in ("http", "https", "socks5", "socks5h"):
        schemes = [u.scheme]
    else:
        schemes = ["http", "socks5"]

    tried = []
    found = None   # (scheme, auth_mode, url)
    for sch in schemes:
        # 先试免密
        url0 = f"{sch}://{host}:{port}"
        ok, detail = await _probe_proxy(url0, timeout=8)
        tried.append({"scheme": sch, "auth": "none", "ok": ok, "detail": detail})
        if ok:
            found = (sch, "none", url0)
            break
        # 免密不通且输入带账密 -> 再试带密
        if cred:
            url1 = f"{sch}://{cred}{host}:{port}"
            ok1, detail1 = await _probe_proxy(url1, timeout=8)
            tried.append({"scheme": sch, "auth": "required", "ok": ok1, "detail": detail1})
            if ok1:
                found = (sch, "required", url1)
                break

    if not found:
        return {"ok": False, "error": "所有组合都连不通(可能是 IP 未加白名单/需账号密码/代理已失效)",
                "tried": tried, "need_auth_hint": not cred}

    sch, auth_mode, url = found
    is_socks = sch.startswith("socks")
    browser_ok = not (is_socks and auth_mode == "required")  # Patchright 不支持带密 SOCKS5
    geo = await _proxy_geo(url)              # 经该代理查出口 IP 归属地
    return {
        "ok": True, "scheme": sch, "auth": auth_mode,
        "recommend": url, "browser_ok": browser_ok, "tried": tried,
        "geo": geo, "geo_text": _geo_text(geo),
        "note": ("HTTP 代理,浏览器与直连都支持" if not is_socks
                 else ("免密 SOCKS5,浏览器与直连都支持" if browser_ok
                       else "带密 SOCKS5:小红书直连/下载可用,但浏览器抓取/登录不支持(建议改用该节点的 HTTP 端口)")),
    }

def _is_mainland(country: str, region: str, city: str) -> bool:
    if country not in ("中国", "China", "CN"):
        return False
    blob = (region or "") + (city or "")
    return not any(x in blob for x in ("香港", "澳门", "澳門", "台湾", "台灣",
                                       "Hong Kong", "Macau", "Taiwan"))

def _geo_loc(p) -> str:
    return " · ".join([x for x in (p.country, p.region, p.city) if x])

def _pool_dict(p: ProxyPool, used: int = 0) -> dict:
    return {
        "id": p.id, "label": p.label, "url": _mask_proxy(p.url),
        "url_full": p.url, "enabled": p.enabled, "status": p.status,
        "note": p.note, "used_by": used,
        "exit_ip": p.exit_ip, "country": p.country, "region": p.region,
        "city": p.city, "isp": p.isp,
        "geo_loc": _geo_loc(p), "is_mainland": _is_mainland(p.country, p.region, p.city),
        "geo_checked": bool(p.exit_ip),
        "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
    }
