"""把 quote-video 的一期成片发到视频号（原创声明 + 含AI生成内容 + 不显示位置）。

用法:
    python publish_episode.py <episode目录> [account_id] [--dry]

标题/正文/话题都从该期产物里读，不手写：
    标题 <- episode.json 的 video_title（publish.py 会按平台规则清洗，不合格则留空）
    正文 <- episode.json 的 copy
    话题 <- render/publish.txt 的「话题:」行，取不到则用情感线默认标签

⚠️ 发布是异步的：报失败不代表没发出去，**先隔 5 分钟查作品列表再决定是否重试**。
"""
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, r"C:\creatorhub")
from app.browser.manager import BrowserManager  # noqa: E402
from app.platforms.channels.publish import publish_channels  # noqa: E402

DB = r"C:\creatorhub\data\creatorhub.db"
DEFAULT_TAGS = "情感,治愈,成长,情感文案"


def load_account(aid):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM douyinaccount WHERE id=?", (aid,)).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"账号 id={aid} 不存在")
    a = type("A", (), {})()
    for k in row.keys():
        setattr(a, k, row[k])
    return a


def read_episode(d: Path):
    ep = json.loads((d / "episode.json").read_text(encoding="utf-8"))
    video = d / "render" / "final.mp4"
    if not video.exists():
        raise SystemExit(f"成片不存在: {video}")
    tags = ""
    pub = d / "render" / "publish.txt"
    if pub.exists():
        m = re.search(r"话题:\s*\n(.+)", pub.read_text(encoding="utf-8"))
        if m:
            tags = ",".join(t.lstrip("#") for t in m.group(1).split() if t.startswith("#"))
    if not tags:
        hs = ep.get("hashtags")
        tags = ",".join(hs) if hs else DEFAULT_TAGS
    return {
        "title": ep.get("video_title") or ep.get("intro_quote") or "",
        "desc": ep.get("copy", ""),
        "topics": tags,
        "video": str(video),
        "bgm": ep.get("bgm", "(无)"),
    }


async def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    d = Path(sys.argv[1])
    aid = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 2
    dry = "--dry" in sys.argv

    info = read_episode(d)
    acc = load_account(aid)
    size = Path(info["video"]).stat().st_size / 1048576
    print(f"账号  : {acc.nickname} ({acc.platform})")
    print(f"期    : {d.name}")
    print(f"成片  : {size:.1f}MB   BGM: {info['bgm']}")
    print(f"标题  : {info['title']}")
    print(f"话题  : {info['topics']}")
    print(f"正文  : {info['desc'][:60]}…")
    print(f"模式  : {'DRY-RUN（不发）' if dry else '★ 真发布 ★'}\n")

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")
    mgr = BrowserManager(UA, r"C:\creatorhub\data\profiles")
    await mgr.start()
    try:
        ok, url, err = await publish_channels(
            mgr, mgr.identity_for(acc), acc.storage_state or "",
            media_type="video", title=info["title"], desc=info["desc"],
            media_paths=[info["video"]], topics=info["topics"], headed=True,
            declare_original=True, ai_generated=True, dry_run=dry,
        )
        print(f"ok={ok}\n结果={url}\n说明={err}")
        if not ok and not dry:
            print("\n⚠️ 报了失败，但视频号发表是异步的——**先隔 5 分钟同步作品列表确认**，"
                  "确实没有再重试，别直接重发。")
    finally:
        await mgr.stop()


asyncio.run(main())
