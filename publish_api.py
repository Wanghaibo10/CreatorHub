"""把 quote-video 的一期成片用**纯接口**发到视频号(不开浏览器点按钮)。

用法:
    python publish_api.py <episode目录> [account_id] [--dry] [--no-original] [--no-ai]

    --dry   走完上传+转码,打印将要发送的 post_create body,**不调发布接口**。
            (上传只是产生草稿素材,不会出现在作品列表里)

标题/正文/话题都从该期产物里读,不手写:
    短标题 <- episode.json 的 video_title(清洗后不足 6 字则留空,不拿描述硬凑)
    正文   <- episode.json 的 copy
    话题   <- render/publish.txt 的「话题:」行,取不到则用情感线默认标签

⚠️ 发布是异步的:报失败不代表没发出去,**先隔 5 分钟查作品列表再决定是否重试**。
"""
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, r"C:\creatorhub")
from app.platforms.channels.api import (  # noqa: E402
    ChannelsAPI, build_topic_xml, cookies_from_profile, extract_topics,
    probe_video, resolve_finder_id)
from app.platforms.channels.publish import _clean_short_title  # noqa: E402

DB = r"C:\creatorhub\data\creatorhub.db"
PROFILES = r"C:\creatorhub\data\profiles"
DEFAULT_TAGS = ["情感", "治愈", "成长", "情感文案"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")


def load_account(aid: int):
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


def read_episode(d: Path) -> dict:
    ep = json.loads((d / "episode.json").read_text(encoding="utf-8"))
    video = d / "render" / "final.mp4"
    if not video.exists():
        raise SystemExit(f"成片不存在: {video}")
    tags = []
    pub = d / "render" / "publish.txt"
    if pub.exists():
        m = re.search(r"话题:\s*\n(.+)", pub.read_text(encoding="utf-8"))
        if m:
            tags = [t.lstrip("#") for t in m.group(1).split() if t.startswith("#")]
    tags = tags or ep.get("hashtags") or DEFAULT_TAGS
    # 视频号的正文就是「描述 + 话题行」,话题必须内嵌在正文里(finderTopicInfo 靠它切段)
    desc = ep.get("copy", "").rstrip() + "\n" + " ".join(f"#{t}" for t in tags) + "\n"
    short = _clean_short_title(ep.get("video_title") or "")[:16]
    if len(short) < 6:
        short = ""       # 短标题不足 6 字视频号不收,宁可不填
    return {"short_title": short, "desc": desc, "video": str(video),
            "bgm": ep.get("bgm", "(无)"), "tags": tags}


async def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    d = Path(sys.argv[1])
    aid = next((int(a) for a in sys.argv[2:] if a.isdigit()), 2)
    dry = "--dry" in sys.argv
    original = "--no-original" not in sys.argv
    ai = "--no-ai" not in sys.argv

    info = read_episode(d)
    acc = load_account(aid)
    meta = probe_video(info["video"])

    print(f"账号  : {acc.nickname} (id={aid})")
    print(f"期    : {d.name}")
    print(f"成片  : {meta['width']}x{meta['height']} {meta['duration']:.1f}s "
          f"{meta['fileSize']/1048576:.1f}MB   BGM: {info['bgm']}")
    print(f"短标题: {info['short_title'] or '(留空)'}")
    print(f"话题  : {' '.join('#' + t for t in info['tags'])}")
    print(f"正文  : {info['desc'][:60]}…")
    print(f"选项  : 原创={original}  含AI生成内容={ai}  位置=不显示")
    print(f"模式  : {'DRY-RUN(上传但不发布)' if dry else '★ 真发布 ★'}\n")

    print("取登录态(headless,只读 cookie 不点任何东西)…")
    cookie, uin = await cookies_from_profile(acc, PROFILES, UA)
    finder_id = await resolve_finder_id(cookie, uin, UA, acc.proxy or None)
    print(f"  uin={uin}  finder_id={finder_id[:28]}…\n")

    async with ChannelsAPI(cookie, finder_id, uin, UA, acc.proxy or None) as api:
        if dry:
            # dry 模式:上传 + 转码走真的(否则测不出链路),但**绝不调 post_create**。
            # 上传产生的只是草稿素材,不会出现在作品列表。
            import time
            params = await api.upload_params()
            print(f"authKey: {params['authKey'][:40]}…")
            trace_key = await api.trace_key()
            t0 = int(time.time())
            print("上传视频…")
            vurl = await api.upload_file(
                Path(info["video"]).read_bytes(), params,
                file_type=params["videoFileType"], file_key=Path(info["video"]).name)
            from app.platforms.channels.api import extract_cover
            cov = Path(info["video"]).with_name(".dry_cover.jpg")
            extract_cover(info["video"], cov)
            print(f"上传封面({cov.stat().st_size} B)…")
            curl = await api.upload_file(
                cov.read_bytes(), params,
                file_type=params["pictureFileType"], file_key="finder_video_img.jpeg")
            cov.unlink(missing_ok=True)
            t1 = int(time.time())
            trace = {"traceKey": trace_key, "uploadCdnStart": t0, "uploadCdnEnd": t1}
            clip = await api.clip_video(vurl, meta, trace)
            print(f"clipKey={clip}  等转码…")
            print("转码结果:", await api.wait_clip(clip))

            body = {"videoClipTaskId": clip, "postFlag": 1 if original else 0,
                    "topics": extract_topics(info["desc"]),
                    "url": vurl[:90] + "…", "cover": curl[:90] + "…",
                    "shortTitle": info["short_title"],
                    "finderTopicInfo": build_topic_xml(info["desc"])}
            print("\n=== 将要发送的关键字段(未发送) ===")
            print(json.dumps(body, ensure_ascii=False, indent=2))
            print("\nDRY-RUN 结束,post_create 未调用。")
            return

        data = await api.publish_video(
            info["video"], info["desc"], short_title=info["short_title"],
            original=original, ai_generated=ai, on_step=lambda s: print(" •", s))
        print("\n发布接口返回:", json.dumps(data, ensure_ascii=False)[:400])
        print("\n⚠️ 视频号发表是异步的 —— 列表里暂时查不到是正常的,"
              "隔 5 分钟再同步确认,别直接重发。")


asyncio.run(main())
