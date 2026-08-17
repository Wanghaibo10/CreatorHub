"""发布后核对线上作品。纯接口查 post_list —— 顺便验证这个接口的参数。"""
import asyncio, sqlite3, sys, datetime, json
sys.path.insert(0, r"C:\creatorhub")
from app.platforms.channels.api import ChannelsAPI, cookies_from_profile, resolve_finder_id

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")


def load_account(aid=2):
    con = sqlite3.connect(r"C:\creatorhub\data\creatorhub.db"); con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM douyinaccount WHERE id=?", (aid,)).fetchone(); con.close()
    a = type("A", (), {})()
    for k in row.keys():
        setattr(a, k, row[k])
    return a


async def main():
    acc = load_account()
    cookie, uin = await cookies_from_profile(acc, r"C:\creatorhub\data\profiles", UA)
    fid = await resolve_finder_id(cookie, uin, UA, acc.proxy or None)
    async with ChannelsAPI(cookie, fid, uin, UA, acc.proxy or None) as api:
        try:
            d = await api.call("post/post_list", {"pageSize": 10, "currentPage": 1,
                                                  "onlyUnread": False})
        except Exception as e:
            print("post_list 调用失败:", e); return
        items = d.get("list") or d.get("post_list") or d.get("object_list") or []
        print(f"=== 线上最近 {len(items)} 条 ===")
        for it in items[:8]:
            d0 = it.get("desc") or it.get("objectDesc") or {}
            desc = d0.get("description", "") if isinstance(d0, dict) else str(d0)
            ct = it.get("createTime") or it.get("create_time") or 0
            t = datetime.datetime.fromtimestamp(int(ct)).strftime("%m-%d %H:%M:%S") if int(ct or 0) > 1e9 else str(ct)
            print(f"  [{t}] {desc[:44]}")
        print("\n=== 前两条的原创/AI标注状态 ===")
        for it in items[:2]:
            d0 = it.get("desc") or {}
            head = (d0.get("description","") if isinstance(d0,dict) else "")[:22]
            keys = {k: v for k, v in it.items()
                    if any(s in k.lower() for s in ("original","postflag","tag","flag","status"))
                    and not isinstance(v, (dict, list))}
            print(f"  「{head}…」")
            print(f"     {keys}")
            for k in ("tagInfo", "originalInfo", "postFlag"):
                if isinstance(it.get(k), (dict, list)):
                    print(f"     {k} = {json.dumps(it[k], ensure_ascii=False)[:200]}")


asyncio.run(main())
