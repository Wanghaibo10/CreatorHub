"""通知渠道 API。

从 main.py 抽出(2026-08-17 模块化)。渠道类型与实际发送在 app/notifier.py,
本模块只做增删改查与「发一条测试消息」。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from moss.common.db import get_session
from moss.model import NotificationChannel
from moss.common.notifier import CHANNEL_TYPES, send_one

router = APIRouter(tags=["notifications"])


class ChannelIn(BaseModel):
    name: str = ""
    type: str
    config: Dict[str, Any] = {}
    enabled: bool = True


class ChannelUpdate(BaseModel):
    name: str | None = None
    config: Dict[str, Any] | None = None
    enabled: bool | None = None


def _channel_dict(c: NotificationChannel) -> dict:
    try:
        cfg = json.loads(c.config or "{}")
    except Exception:
        cfg = {}
    return {"id": c.id, "name": c.name, "type": c.type,
            "enabled": c.enabled, "config": cfg}


@router.get("/api/notifications")
async def list_channels():
    with get_session() as s:
        return [_channel_dict(c) for c in s.exec(select(NotificationChannel)).all()]


@router.post("/api/notifications")
async def add_channel(body: ChannelIn):
    if body.type not in CHANNEL_TYPES:
        raise HTTPException(400, f"渠道类型须为 {CHANNEL_TYPES}")
    with get_session() as s:
        c = NotificationChannel(name=body.name or body.type, type=body.type,
                                config=json.dumps(body.config), enabled=body.enabled)
        s.add(c); s.commit(); s.refresh(c)
        return _channel_dict(c)


@router.put("/api/notifications/{cid}")
async def update_channel(cid: int, body: ChannelUpdate):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if not c:
            raise HTTPException(404)
        if body.name is not None:
            c.name = body.name
        if body.config is not None:
            c.config = json.dumps(body.config)
        if body.enabled is not None:
            c.enabled = body.enabled
        s.add(c); s.commit(); s.refresh(c)
        return _channel_dict(c)


@router.delete("/api/notifications/{cid}")
async def del_channel(cid: int):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if c:
            s.delete(c); s.commit()
    return {"ok": True}


@router.post("/api/notifications/{cid}/test")
async def test_channel(cid: int):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if not c:
            raise HTTPException(404)
        ch_type, cfg = c.type, json.loads(c.config or "{}")
    ok, detail = await send_one(ch_type, cfg, "CreatorHub · 测试通知",
                                "这是一条测试消息,收到说明渠道配置正常 ✓")
    return {"ok": ok, "detail": detail}
