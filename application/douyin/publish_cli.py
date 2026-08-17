#!/usr/bin/env python3
"""用纯协议发一条抖音作品(零浏览器)。

    python -m application.douyin.publish_cli <视频> <标题> [--desc 文案] [--account 1] [--dry]

登录态从本仓 sqlite 读。--dry 只上传不 create。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from application.douyin.api import (  # noqa: E402
    DEFAULT_UA, DouyinAPI, cookies_from_account)

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
    args = ap.parse_args()

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

    async with DouyinAPI(ck, ua, acc.proxy or None) as api:
        me = await api.ping()
        print(f"  • 登录 {me.get('nickname')} / {me.get('unique_id')}")
        up = await api.upload_video(args.video, on_step=lambda s: print("  •", s))
        print(f"\nvid={up.get('vid')}  poster={up.get('poster')}")
        if args.dry:
            print("DRY-RUN 结束,create 未调用。")
            return 0
        res = await api.create(
            up, args.title, desc, visibility=args.visibility,
            allow_save=not args.no_save)
        print(f"create_v2: status={res.get('status_code')} "
              f"item_id={res.get('item_id') or ''} {res.get('status_msg') or ''}")
        return 0 if res.get("status_code") in (0, None) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
