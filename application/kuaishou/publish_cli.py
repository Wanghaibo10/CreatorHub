#!/usr/bin/env python3
"""用纯接口发一条快手作品(零浏览器)。

    python -m application.kuaishou.publish_cli <视频> <文案> [--account 4] [--dry]

登录态从本仓 sqlite 读(只读,不启动服务、不抢 profile 锁)。
路径用 --db 覆盖,或设 CREATORHUB_DB。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

# 仓库根(本文件在 application/<平台>/ 下两层)
ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT))
from application.kuaishou.api import (  # noqa: E402
    DEFAULT_UA, KuaishouAPI, _ffprobe_duration_ms, cookies_from_account,
    sign_available)

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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("caption")
    ap.add_argument("--account", type=int, default=4)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry", action="store_true", help="只上传不发布")
    args = ap.parse_args()

    ok, why = sign_available()
    print(f"签名器: {'✅ ' + why if ok else '❌ ' + why}")
    if not ok:
        return 1

    acc = load_account(args.db, args.account)
    if (acc.platform or "") != "kuaishou":
        raise SystemExit(f"账号 {acc.nickname} 不是快手账号({acc.platform})")
    ck = cookies_from_account(acc)
    if not ck:
        raise SystemExit("storage_state 里没有快手 cookie —— 先在本服务扫码登录")

    print(f"账号  : {acc.nickname} (id={args.account})")
    print(f"成片  : {args.video}  {Path(args.video).stat().st_size / 1048576:.1f}MB")
    print(f"文案  : {args.caption[:70]}")
    print(f"登录态: {len(ck)} 个 cookie(没开浏览器)")
    print(f"模式  : {'DRY-RUN(上传但不发布)' if args.dry else '★ 真发布 ★'}\n")

    async with KuaishouAPI(ck, DEFAULT_UA, acc.proxy or None) as api:
        up = await api.upload_video(args.video, on_step=lambda s: print("  •", s))
        print(f"\nfileId={up.get('fileId')}  coverKey={up.get('coverKey')}")
        if args.dry:
            print("DRY-RUN 结束,submit 未调用(不进作品列表)。")
            return 0
        res = await api.submit(up, args.caption,
                               duration_ms=_ffprobe_duration_ms(args.video))
        print(f"submit: result={res.get('result')} {res.get('message')}")
        return 0 if res.get("result") == 1 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
