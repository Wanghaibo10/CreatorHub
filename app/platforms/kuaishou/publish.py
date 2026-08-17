"""快手发布入口(浏览器自动化快手创作者服务平台 cp.kuaishou.com)。

快手 PC 端没有像小红书那样成熟的 execjs 签名直发方案,这里走浏览器自动化:
用账号专属持久 profile(已含创作者登录态)打开发布页,上传文件、填文案、点发布。

⚠️ 实验性:发布页选择器随快手改版可能失效,集中在下面的 _* 选择器常量;
   发布时弹真实窗口,遇验证码/需补封面可在窗口里手动处理。

2026-08-16 修了两个都会让发布**静默失败**的坑(实机抓包定位):

① **`button:has-text("发布")` 会点到左侧导航的「发布作品」菜单**。
   页面上含「发布」的元素至少四个:侧边栏「发布作品」(x≈52)、顶部「发布视频」、
   「发布设置」「发布时间」小标题,真正的提交按钮在主区域**最底部**(x≈500,y≈1258)。
   旧代码点开了侧边栏菜单,然后一直等「发布成功」等到超时,报的却是
   「未找到发布按钮(发布页可能改版)」—— 按钮找到了,只是找错了那个。

② **固定 `wait_for_timeout(6000)` 等上传**。64MB 的片子实测传 35 秒;
   6 秒后表单还没就绪。等待时长必须由**事件**决定,不能由猜测决定 ——
   这里改成等 `upload/finish` 这个响应真的回来。

判据落在**位置**上(x≥260 排除侧边栏、取最靠下的那个),不落在文本上:
文本「发布」在这个页面根本不唯一,而位置能把导航和表单分开。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

VIDEO_URL = "https://cp.kuaishou.com/article/publish/video"
IMAGE_URL = "https://cp.kuaishou.com/article/publish/atlas"
_DESC_SEL = ['div[contenteditable="true"]', '#work-description-edit',
             'textarea[placeholder*="描述"]', '.editor-content', 'textarea']
#: 侧边栏宽度上限。x 小于它的「发布」一律是导航,不是提交按钮。
_SIDEBAR_X = 260
#: 上传完成的标志:这个响应回来才说明视频已落到快手那边
_FINISH_API = "upload/finish"


async def _click_first(page, selectors, timeout=2500) -> bool:
    for sel in selectors:
        try:
            await page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _fill_first(page, selectors, text, timeout=2500) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.click(timeout=timeout)
            await el.fill(text, timeout=timeout)
            return True
        except Exception:
            try:
                await page.keyboard.type(text)
                return True
            except Exception:
                continue
    return False


async def _find_submit_button(page):
    """主区域里那个真正的提交按钮 —— 按**位置**挑,不按文本挑。

    返回 locator 或 None。挑法:所有可见的「发布…」元素里,x≥_SIDEBAR_X
    (排除侧边栏导航)且 y 最大(表单提交按钮总在最底下)。
    """
    import re as _re
    cands = []
    for el in await page.get_by_text(_re.compile(r"^\s*发布")).all():
        try:
            if not await el.is_visible():
                continue
            box = await el.bounding_box()
            if box and box["x"] >= _SIDEBAR_X:
                cands.append((box["y"], el))
        except Exception:
            continue
    if not cands:
        return None
    return max(cands, key=lambda c: c[0])[1]


async def publish_kuaishou(mgr: BrowserManager, identity: Identity,
                           storage_state_json: str, media_type: str, title: str,
                           desc: str, media_paths: List[str], topics: str = "",
                           headed: bool = True, timeout_seconds: int = 600
                           ) -> Tuple[bool, str, str]:
    """发布一条快手作品。返回 (ok, result_url, error)。
    storage_state_json 仅用于校验(实际登录态在该账号持久 profile 里)。

    ⚠️ `timeout_seconds` 默认从 180 提到 600:它现在盖的是**整条上传**,
    而 64MB 实测 35 秒、慢网络会更久。宁可等,不要传一半就放弃。
    """
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]
    # 快手正文:标题 + 描述 + 话题
    body = ((title + "\n" if title else "") + (desc or "")
            + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()[:1000]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    #: 视口要够大 —— 提交按钮在 y≈1258,默认视口下它在屏幕外,
    #: bounding_box 拿不到就选不中。
    try:
        await page.set_viewport_size({"width": 1600, "height": 1100})
    except Exception:
        pass
    uploaded = {"done": False}
    page.on("response", lambda r: uploaded.__setitem__("done", True)
            if _FINISH_API in r.url else None)

    ok, result_url, error = False, "", ""
    try:
        url = VIDEO_URL if media_type == "video" else IMAGE_URL
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2500)
        if "passport" in page.url or "/login" in page.url:
            return False, "", "logged_out:快手创作平台未登录"
        try:
            await page.locator('input[type="file"]').first.set_input_files(
                files if media_type == "images" else files[:1], timeout=30000)
        except Exception as e:
            return False, "", f"上传文件失败: {e!r}"

        #: 等上传真的完成 —— 由 `upload/finish` 这个事件决定,不是固定秒数
        waited = 0
        while not uploaded["done"] and waited < timeout_seconds:
            await page.wait_for_timeout(3000)
            waited += 3
        if not uploaded["done"]:
            return False, "", f"上传未在 {timeout_seconds}s 内完成(片子太大或网络慢)"
        #: 上传完还要等快手抽封面(它在轮询 cover/edit/recommend/query),
        #: 封面没就绪时提交按钮点了没反应
        await page.wait_for_timeout(25000)

        if body:
            await _fill_first(page, _DESC_SEL, body)
        await page.wait_for_timeout(1500)

        btn = await _find_submit_button(page)
        if btn is None:
            return False, "", "主区域找不到提交按钮(发布页可能改版)"
        try:
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(800)
            await btn.click(timeout=8000)
        except Exception as e:
            return False, "", f"点提交失败: {e!r}"

        for _ in range(30):
            await page.wait_for_timeout(4000)
            if await page.get_by_text("发布成功", exact=False).count():
                ok = True
                break
            if "manage" in page.url or "works" in page.url:
                ok = True                      # 发布成功会跳视频管理页
                break
        result_url = page.url if ok else ""
        if not ok:
            error = "已点发布但未确认成功(请到快手创作平台确认)"
    except Exception as e:
        error = f"发布异常: {e!r}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
