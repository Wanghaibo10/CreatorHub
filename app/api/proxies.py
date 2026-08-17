"""代理池 API。

从 main.py 抽出(2026-08-17 模块化)。探测/归属地等领域逻辑在 services/proxy.py,
本模块只做 HTTP 出入参与持久化。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from moss.common.db import get_session
from moss.model import DouyinAccount, ProxyPool
from app.service.proxy import (_detect_proxy, _geo_loc, _geo_text, _is_mainland, _mask_proxy, _pool_dict, _probe_proxy, _proxy_geo, _proxy_probe_status, _proxy_status_from_detail)

router = APIRouter(tags=["proxies"])


class PoolProxyUpdate(BaseModel):
    """PUT 的入参:四个字段都可选,只改传了的那些(None = 不动)。"""
    url: str | None = None
    label: str | None = None
    note: str | None = None
    enabled: bool | None = None


class ProxyDetectIn(BaseModel):
    url: str

class ProxyIn(BaseModel):
    proxy: str = ""

class PoolProxyIn(BaseModel):
    url: str
    label: str = ""
    note: str = ""
    enabled: bool = True
    geo: Dict[str, Any] | None = None       # 判别得到的归属地(可选,建库时一并写入)

class ProxyImportIn(BaseModel):
    text: str = ""        # 多行,每行一个代理(可带 # 注释、空行)

@router.post("/api/proxies/detect")
async def detect_proxy(body: ProxyDetectIn):
    return await _detect_proxy(body.url)

@router.get("/api/proxies")
async def list_proxies():
    with get_session() as s:
        rows = s.exec(select(ProxyPool).order_by(ProxyPool.id)).all()
        used = {}
        for a in s.exec(select(DouyinAccount)).all():
            if a.proxy:
                used[a.proxy] = used.get(a.proxy, 0) + 1
        return [_pool_dict(p, used.get(p.url, 0)) for p in rows]

@router.post("/api/proxies")
async def add_proxy(body: PoolProxyIn):
    from application.browser.manager import _parse_proxy, normalize_proxy
    url = (body.url or "").strip()
    if not url or not _parse_proxy(url):
        raise HTTPException(400, "代理格式无法解析,示例:http://user:pass@host:port 或 socks5://host:port")
    url = normalize_proxy(url)
    with get_session() as s:
        if s.exec(select(ProxyPool).where(ProxyPool.url == url)).first():
            raise HTTPException(409, "该代理已在池中")
        g = body.geo or {}
        p = ProxyPool(url=url, label=body.label.strip(), note=body.note.strip(),
                      enabled=body.enabled,
                      exit_ip=g.get("ip", ""), country=g.get("country", ""),
                      region=g.get("region", ""), city=g.get("city", ""),
                      isp=g.get("isp", ""))
        s.add(p); s.commit(); s.refresh(p)
        return _pool_dict(p)

@router.post("/api/proxies/import")
async def import_proxies(body: ProxyImportIn):
    """批量粘贴多行导入代理池。每行一个,支持 # 注释/空行,自动校验+去重。"""
    from application.browser.manager import _parse_proxy, normalize_proxy
    added = skipped = invalid = 0
    invalid_lines = []
    with get_session() as s:
        existing = {p.url for p in s.exec(select(ProxyPool)).all()}
        seen = set(existing)
        for raw in (body.text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # 允许 "备注,地址" 或 "备注 地址" 形式;否则整行当地址
            label, url = "", line
            for sep in (",", "\t", " | ", " "):
                if sep in line:
                    a, b = line.split(sep, 1)
                    if _parse_proxy(b.strip()):
                        label, url = a.strip(), b.strip()
                    break
            url = normalize_proxy(url.strip())
            if not _parse_proxy(url):
                invalid += 1
                if len(invalid_lines) < 8:
                    invalid_lines.append(line[:60])
                continue
            if url in seen:
                skipped += 1
                continue
            s.add(ProxyPool(url=url, label=label))
            seen.add(url)
            added += 1
        if added:
            s.commit()
    return {"ok": True, "added": added, "skipped": skipped, "invalid": invalid,
            "invalid_samples": invalid_lines}

@router.put("/api/proxies/{pid}")
async def update_proxy(pid: int, body: PoolProxyUpdate):
    from application.browser.manager import _parse_proxy, normalize_proxy
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            raise HTTPException(404, "代理不存在")
        if body.url is not None:
            url = body.url.strip()
            if not url or not _parse_proxy(url):
                raise HTTPException(400, "代理格式无法解析")
            p.url = normalize_proxy(url)
        if body.label is not None:
            p.label = body.label.strip()
        if body.note is not None:
            p.note = body.note.strip()
        if body.enabled is not None:
            p.enabled = body.enabled
        s.add(p); s.commit(); s.refresh(p)
        return _pool_dict(p)

@router.delete("/api/proxies/{pid}")
async def del_proxy(pid: int):
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            return {"ok": True}
        used = len(s.exec(select(DouyinAccount.id)
                          .where(DouyinAccount.proxy == p.url)).all())
        s.delete(p); s.commit()
    return {"ok": True, "still_used_by": used}

@router.post("/api/proxies/{pid}/test")
async def test_proxy_entry(pid: int):
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if not p:
            raise HTTPException(404, "代理不存在")
        url = p.url
    ok, detail = await _probe_proxy(url)
    geo = await _proxy_geo(url) if ok else None
    geo_text = _geo_text(geo)
    with get_session() as s:
        p = s.get(ProxyPool, pid)
        if p:
            p.status = _proxy_status_from_detail(ok, detail)
            p.last_checked_at = datetime.utcnow()
            if geo:                            # 归属地写入结构化字段(供「地区」列展示)
                p.exit_ip = geo.get("ip", "") or p.exit_ip
                p.country = geo.get("country", "") or p.country
                p.region = geo.get("region", "") or p.region
                p.city = geo.get("city", "") or p.city
                p.isp = geo.get("isp", "") or p.isp
            s.add(p); s.commit()
    return {"ok": ok, "detail": detail, "geo": geo, "geo_text": geo_text}

@router.get("/api/proxies/options")
async def proxy_options():
    """供账号/登录「选代理」下拉用:返回全部代理(启用优先,含停用并标记)。
    手动选可选任意一条;auto/批量分配仍只用启用的(见 assign_proxy_from_pool)。"""
    with get_session() as s:
        rows = s.exec(select(ProxyPool)
                      .order_by(ProxyPool.enabled.desc(), ProxyPool.id)).all()
        used = {}
        for a in s.exec(select(DouyinAccount)).all():
            if a.proxy:
                used[a.proxy] = used.get(a.proxy, 0) + 1
        return [{"id": p.id, "label": p.label or _mask_proxy(p.url),
                 "url": p.url, "masked": _mask_proxy(p.url),
                 "status": p.status, "enabled": p.enabled,
                 "used_by": used.get(p.url, 0)} for p in rows]

