#!/usr/bin/env python3
"""用纯协议发一条抖音作品(零浏览器)。

    python -m application.douyin.publish_cli <视频> <标题> [--desc 文案] [--account 1] [--dry]

登录态从本仓 sqlite 读。--dry 只上传不 create。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from application.douyin.api import (  # noqa: E402
    DEFAULT_UA, DouyinAPI, NeedVerify, cookies_from_account)

DEFAULT_DB = os.environ.get("CREATORHUB_DB", str(ROOT / "data" / "creatorhub.db"))


def load_account(db: str, aid: int):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM douyinaccount WHERE id=?", (aid,)).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"账号 id={aid} 不在 {db}")
    acc = type("A", (), {})()
    for k in row.keys():
        setattr(acc, k, row[k])
    return acc


def _first_douyin_id(db: str) -> int:
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT id FROM douyinaccount WHERE platform=? ORDER BY id",
        ("douyin",)).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"{db} 里没有抖音号")
    return int(row[0])


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("title")
    ap.add_argument("--desc", default="", help="正文/简介,默认等于标题")
    ap.add_argument("--account", type=int, default=0)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--visibility", default="public",
                    choices=("public", "friends", "private"))
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只上传不发布")
    ap.add_argument("--code", default="",
                    help="短信验证码(6 位)。配合 --resume 用,不重传视频")
    ap.add_argument("--resume", default="",
                    help="上次被 need_verify 挡下时存的 pending json")
    args = ap.parse_args()

    if args.resume:
        return await _resume(args)
    if not Path(args.video).is_file():
        raise SystemExit(f"视频不在:{args.video}")
    aid = args.account or _first_douyin_id(args.db)
    acc = load_account(args.db, aid)
    if (acc.platform or "") != "douyin":
        raise SystemExit(f"账号 {acc.nickname} 不是抖音({acc.platform})")
    ck = cookies_from_account(acc)
    if "sessionid" not in ck:
        raise SystemExit("storage_state 没有 sessionid —— 先创作者登录")

    desc = args.desc or args.title
    ua = acc.ua or DEFAULT_UA
    print(f"账号  : {acc.nickname} (id={aid})")
    print(f"成片  : {args.video}  {Path(args.video).stat().st_size / 1048576:.1f}MB")
    print(f"标题  : {args.title[:30]}")
    print(f"登录态: {len(ck)} 个 cookie")
    print(f"模式  : {'DRY-RUN(上传但不发布)' if args.dry else '★ 真发布 ★'}\n")

    #: ⚠️ **必须把 storage_state 传进去** —— 写操作的 bd-ticket-guard 签名
    #: 材料就在它的 `creator.douyin.com` localStorage 里。少了这一个参数,
    #: 上传全程正常(读接口不要签名)、直到最后一步 `create_v2` 才抛
    #: `TicketGuardUnavailable: 缺 ticket-guard 材料`,看起来像「登录态没了」
    #: 而实际材料一直在库里(2026-08-18 产片机实跑:12 个分片传完才炸)。
    #: `publish_via_http` 那条入口传了、这条没传 —— 同一条能力两个入口
    #: 只接通一个,是「声明了不等于接通了」的老形状。
    async with DouyinAPI(ck, ua, acc.proxy or None,
                         storage_state=(acc.creator_storage_state
                                        or acc.storage_state)) as api:
        me = await api.ping()
        print(f"  • 登录 {me.get('nickname')} / {me.get('unique_id')}")
        up = await api.upload_video(args.video, on_step=lambda s: print("  •", s))
        print(f"\nvid={up.get('vid')}  poster={up.get('poster')}")
        if args.dry:
            print("DRY-RUN 结束,create 未调用。")
            return 0
        try:
            res = await api.create(
                up, args.title, desc, visibility=args.visibility,
                allow_save=not args.no_save)
        except NeedVerify as exc:
            #: 风控要短信验证。**视频已经传完了** —— 把 vid 连同这次的
            #: 标题/文案一起存下,验证过了走 `--resume` 直接 create,
            #: 不重传 45MB(2026-08-18 实跑:一次上传 12 个分片约 2 分钟)。
            return await _on_need_verify(api, exc, up, args, desc, aid)
        print(f"create_v2: status={res.get('status_code')} "
              f"item_id={res.get('item_id') or ''} {res.get('status_msg') or ''}")
        return 0 if res.get("status_code") in (0, None) else 1


def _pending_path(aid: int) -> Path:
    return ROOT / "data" / f"douyin-pending-{aid}.json"


async def _on_need_verify(api, exc, up: dict, args, desc: str, aid: int) -> int:
    """被 need_verify 挡下:存住上传结果 + 发验证码,告诉人下一步怎么走。"""
    uid = str((exc.decision or {}).get("encrypt_uid") or "")
    scene = str((exc.decision or {}).get("scene") or "creator")
    pend = {"account": aid, "up": up, "title": args.title, "desc": desc,
            "visibility": args.visibility, "no_save": args.no_save,
            "encrypt_uid": uid, "scene": scene}
    p = _pending_path(aid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pend, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n⚠️ 抖音要求本人短信验证。视频已传完(vid={up.get('vid')}),"
          f"已存 {p.name}")
    if not uid:
        print("   ✗ 响应头里没解出 encrypt_uid —— 只能去创作者中心手动过一次")
        return 2
    try:
        sent = await api.send_verify_code(uid, scene=scene)
    except Exception as e:                                       # noqa: BLE001
        print(f"   ✗ 发码失败({type(e).__name__}: {e}) —— 去创作者中心手动过")
        return 2
    print(f"   ✓ 验证码已发到 {sent.get('mobile') or '绑定手机'}"
          f"(retry_time={sent.get('retry_time')})")
    print(f"   收到后跑:python -m application.douyin.publish_cli "
          f"'' '' --resume {p} --code 六位数字")
    return 3


async def _resume(args) -> int:
    """拿到验证码之后:validate → **直接 create,不重新上传**。"""
    p = Path(args.resume)
    if not p.is_file():
        raise SystemExit(f"pending 不在:{p}")
    pend = json.loads(p.read_text(encoding="utf-8"))
    if not args.code:
        raise SystemExit("要 --code 六位验证码")
    aid = int(pend["account"])
    acc = load_account(args.db, aid)
    ck = cookies_from_account(acc)
    print(f"账号  : {acc.nickname} (id={aid}) / 复用 vid={pend['up'].get('vid')}")
    async with DouyinAPI(ck, acc.ua or DEFAULT_UA, acc.proxy or None,
                         storage_state=(acc.creator_storage_state
                                        or acc.storage_state)) as api:
        ticket = await api.validate_verify_code(
            pend["encrypt_uid"], args.code, scene=pend.get("scene", "creator"))
        print(f"  • 验证通过 ticket={(ticket or '')[:16]}…")
        res = await api.create(
            pend["up"], pend["title"], pend["desc"],
            visibility=pend.get("visibility", "public"),
            allow_save=not pend.get("no_save"))
        print(f"create_v2: status={res.get('status_code')} "
              f"item_id={res.get('item_id') or ''} {res.get('status_msg') or ''}")
        ok = res.get("status_code") in (0, None)
        if ok:
            p.unlink(missing_ok=True)          # 发成功了就别留着诱人重发
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
