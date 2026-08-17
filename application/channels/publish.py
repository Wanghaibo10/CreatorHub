"""视频号发布入口(浏览器自动化视频号助手 channels.weixin.qq.com)。

视频号发布页是 wujie 微前端(内容在 shadowRoot 里)。Patchright 的定位器默认会穿透
**开放的** shadow DOM,故下面用普通 CSS 选择器即可;若视频号把 shadow root 设成 closed
则需改用 CDP pierce(参见 _CHANNELS_* 注释)。

⚠️ 实验性 + 需校准:发布页选择器随视频号改版/wujie 版本变化,集中在下面 _* 常量;
   选择器初值取自对小V猫的逆向观察(`.post-view` 系),务必用真实账号发一条核对。
   发布时弹真实窗口,遇「实名/过脸验证」「封面必填」可在窗口里手动处理。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from application.browser.identity import Identity
from application.browser.manager import BrowserManager
from contextlib import suppress

log = logging.getLogger("creatorhub.channels")

# 视频号发布入口(实测):就是 create 页;视频/图文靠点左侧「视频」「图文」导航切换
# (逆向 server.jsc 里还有 finderNewLifeCreateg,但实测会 302 回 create,故不直接跳它)。
CREATE_URL_VIDEO = "https://channels.weixin.qq.com/platform/post/create"
CREATE_URL = CREATE_URL_VIDEO   # 兼容旧引用

# 标题/描述编辑器(wujie shadowRoot 内)。视频号「短标题」与「描述」是两个框,
# 这里优先填描述编辑器;短标题选择器留作备用。
_DESC_SEL = [
    '.post-view .post-desc-box .input-editor',
    '.post-desc-box .input-editor',
    'div.input-editor[contenteditable="true"]',
    'textarea[placeholder*="描述"]',
    'div[contenteditable="true"]',
]
_SHORT_TITLE_SEL = [
    '.post-view .short-title-wrap input',
    'input[placeholder*="标题"]',
]
# 文件上传 input(视频号发布页可能在 wujie iframe / shadowRoot 里)
_FILE_SEL = [
    '.post-view .upload input[type="file"]',
    '.post-view input[type="file"]',
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
]
# 上传区(点击会弹原生文件对话框,必须用 expect_file_chooser 拦截,不能裸点)。
# 真实 UI:虚线「+」框,内含「上传时长8小时内…」提示文案。
_UPLOAD_ZONE = [
    'text=上传时长', 'text=上传图片', 'text=从这里上传',
    '.center-upload', '.finder-upload', '[class*="upload-entry"]',
    '[class*="upload"]',
]
# 左侧内容管理导航:视频 / 图文(点它进对应「列表页」)
_IMAGE_NAV = [
    'text=图文', 'a:has-text("图文")', 'li:has-text("图文")',
    '[class*="menu"]:has-text("图文")',
]
# 图文列表页里的「发表图文」按钮(在 micro/content iframe 内,必须跨 frame 点)
_CREATE_IMAGE_BTN = ['text=发表图文', 'button:has-text("发表图文")',
                     '[class*="btn"]:has-text("发表图文")']
# 发布按钮
_PUBLISH_BTN = [
    '.post-view .form-btns button.weui-desktop-btn_primary',
    '.post-view .form-btns button',
    'button:has-text("发表")',
    'button:has-text("发布")',
]
# 发布成功判据(页面跳转/出现提示)
_OK_TEXTS = ["发表成功", "发布成功", "提交成功"]

# ── 原创声明 / AI 生成标注（2026-08-01 本地新增，选择器实测自真实账号发布页）──
# 用户「半世清言」的内容全是 AI 生成的原创绘本，两项每期都要勾，故默认打开；
# 要按期区分就给 publish_channels 传 declare_original / ai_generated。
# ⚠️ 勾不上时**不点发表**、直接返回失败——发出去了才发现没标记是撤不回来的。
DEFAULT_DECLARE_ORIGINAL = True
DEFAULT_AI_GENERATED = True

# 表单里「声明原创」那行的 checkbox。页面上 ant-checkbox-wrapper 有好几个，
# 必须靠这句说明文字锚定，不能按序号取。
_ORIGINAL_CHECKBOX = [
    'label.ant-checkbox-wrapper:has-text("声明后，作品将展示原创标记")',
    'label.ant-checkbox-wrapper:has-text("展示原创标记")',
]
# 勾上后弹「原创权益」对话框：先勾协议，再点里面的「声明原创」按钮（勾之前是 _disabled）。
# ⚠️ 必须限定在 .weui-desktop-dialog 内：页面里**还躺着一份隐藏的弹窗模板**，
# 不限定就会点到不可见的那个，症状是「点了没反应、确认按钮一直 disabled」。
_ORIGINAL_PROTO_CHECKBOX = [
    '.weui-desktop-dialog .original-proto-wrapper label.ant-checkbox-wrapper',
    '.weui-desktop-dialog label.ant-checkbox-wrapper',
]
# 确认按钮同样限定在对话框内，且必须按文本认——页面上 weui-desktop-btn_primary
# 到处都是，只按 class 取会点到「确定」之类的别的按钮。
_ORIGINAL_CONFIRM_BTN = ['.weui-desktop-dialog button:has-text("声明原创")']

# 「含AI生成内容」不是页面上直接可点的 tag，而是**「视频标注」下拉里的一个选项**，
# 必须先点开下拉。收起状态下选项在 DOM 里但不可见——直接点会被判可见性失败，
# 或撞上别的浮层拦截 pointer events。
_MARK_TAG_SELECT = ['.mark-tag-select', '[class*="mark-tag-select"]', 'text=选择视频标注']
_AI_TAG = [
    '.mark-tag-option:has-text("含AI生成内容")',
    '[class*="option"]:has-text("含AI生成内容")',
]
# 选中后下拉收起，控件文字由「选择视频标注」变成选中的标注名——以此校验
_MARK_TAG_DISPLAY = ['.mark-tag-select', '[class*="mark-tag-body"]']

# 上一次发布中途退出会留草稿，再进发布页会先弹「保存/不保存」对话框挡住整个表单，
# 症状是找不到上传入口(诊断里 fi=0 up=0，btn 里出现「不保存/保存」)。
# 一律选「不保存」：这次是全新的一条内容，不该继承上次的残留。
_DRAFT_DISCARD = ['button:has-text("不保存")', '.weui-desktop-dialog button:has-text("不保存")']
# 页面挂载判据(wujie 微前端，主内容区出来才谈得上找元素)
_PAGE_READY = ['.post-view', 'input[type="file"]', '[class*="upload"]']

# ── 位置 POI(可选;选择器需真号校准,任何一步失败都跳过、不阻塞发布)──
_POI_TRIGGER = [
    'text=不显示位置', '[class*="position"]', '[class*="location"]',
    '.location-display', '.position-select',
]
_POI_INPUT = [
    'input[placeholder*="搜索位置"]', 'input[placeholder*="搜索地点"]',
    'input[placeholder*="位置"]', '[class*="position"] input',
    '[class*="location"] input',
]
_POI_RESULT = [
    '[class*="position"] [class*="item"]', '[class*="location"] [class*="item"]',
    '[class*="poi"] [class*="item"]', '[class*="position"] li',
    '.dropdown-item', '[class*="option"]',
]
# 位置当前值的显示区（点它展开下拉），以及下拉里的「不显示位置」项
_POI_DISPLAY = ['.position-display', '[class*="position-display"]', '.post-position-wrap']
_POI_NONE = [
    '.location-item:has-text("不显示位置")',
    '.option-item:has-text("不显示位置")',
    'text=不显示位置',
]


async def _fill_first(page, selectors, text, timeout=2500) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.click(timeout=timeout)
            try:
                await el.fill(text, timeout=timeout)
            except Exception:
                await page.keyboard.type(text, delay=30)   # contenteditable 不支持 fill
            return True
        except Exception:
            continue
    return False


async def _click_first(page, selectors, timeout=3000) -> bool:
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def _click_in_frames(page, selectors, timeout=4000) -> bool:
    """在主页面 + 所有子 frame(micro/content iframe)里点第一个命中的元素。
    视频号发布 UI 在 iframe 里,普通 page.click 点不到,必须遍历 frames。"""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


async def _clear_location(page) -> bool:
    """把位置设成「不显示位置」。

    ⚠️ 视频号会**按出口 IP 自动填一个位置**（实测自动填了「北京市」），不主动清就会
    连同位置一起发出去。所以「没传 location」的语义必须是「明确不显示」，不能放任。
    实测(2026-08-01 真号)：点 .position-display 展开，列表第一项就是「不显示位置」。
    """
    trig, _tf = await _find_visible(page, _POI_DISPLAY)
    if trig is None:
        log.warning("[channels_publish] 未找到位置控件，无法清除位置")
        return False
    try:
        if "不显示位置" in ((await trig.inner_text()) or ""):
            return True
        await trig.click(timeout=5000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("[channels_publish] 展开位置下拉失败: %r", e)
        return False
    opt, _of = await _find_visible(page, _POI_NONE)
    if opt is None:
        log.warning("[channels_publish] 位置下拉里没找到「不显示位置」")
        return False
    try:
        await opt.click(timeout=5000)
        await page.wait_for_timeout(1800)
    except Exception as e:
        log.warning("[channels_publish] 点「不显示位置」失败: %r", e)
        return False
    disp, _df = await _find_visible(page, _POI_DISPLAY)
    try:
        ok = disp is not None and "不显示位置" in ((await disp.inner_text()) or "")
    except Exception:
        ok = False
    log.info("[channels_publish] 清除位置: %s", ok)
    return ok


async def _set_location(page, location: str):
    """设置视频号位置 POI(可选,best-effort)。任何一步失败都只记日志、跳过,不影响发布。
    流程:点开位置控件 -> 输入搜索 -> 点第一个结果。选择器需真号校准(见 _POI_*)。
    不传 location 时**主动清成「不显示位置」**——见 _clear_location 的说明。"""
    if not location:
        await _clear_location(page)
        return
    try:
        if not await _click_in_frames(page, _POI_TRIGGER, timeout=3000):
            log.warning("[channels_publish] 未找到位置控件,跳过位置设置")
            return
        await page.wait_for_timeout(1200)
        inp, _fr = await _find_in_frames(page, _POI_INPUT)
        if inp is None:
            log.warning("[channels_publish] 未找到位置搜索框,跳过位置(位置选择器需校准 _POI_INPUT)")
            return
        await inp.click()
        await page.keyboard.type(location, delay=40)
        await page.wait_for_timeout(2200)      # 等搜索结果回来
        if not await _click_in_frames(page, _POI_RESULT, timeout=3000):
            log.warning("[channels_publish] 位置「%s」无匹配结果或结果选择器需校准 _POI_RESULT", location)
        else:
            log.info("[channels_publish] 已设置位置: %s", location)
    except Exception as e:
        log.warning("[channels_publish] 设置位置异常(已跳过): %r", e)


def _clean_short_title(s: str) -> str:
    """把任意文案洗成视频号短标题能接受的样子。

    视频号原话（2026-08-01 实测红字）：「标题包含特殊字符，符号仅支持书名号、引号、
    冒号、加号、问号、百分号、摄氏度，逗号可用空格代替」。
    片头金句基本都带逗号句号，不洗的话每期都会卡在这一步发不出去。
    """
    s = re.sub(r"[，,、]", " ", s or "")            # 逗号顿号 -> 空格（平台明说可代替）
    s = re.sub(r"[^\w一-鿿《》「」“”\"':：+＋?？%％℃ ]", "", s)   # 只留白名单
    return re.sub(r"\s+", " ", s).strip()


async def _find_visible(page, selectors):
    """只返回**可见**的元素。发布页里隐藏的弹窗模板 / 收起的下拉选项都在 DOM 里，
    _find_in_frames 不判可见性会抓到它们，点了毫无效果——这两个开关必须用这个。"""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return loc, frame
            except Exception:
                continue
    return None, None


async def _declare_original(page) -> Tuple[bool, str]:
    """勾「声明原创」。返回 (成功, 说明)。

    实测路径（2026-08-01 真号验证）：表单勾 checkbox -> 弹「原创权益」对话框 ->
    勾对话框内的协议 checkbox（此时「声明原创」按钮才从 disabled 转可用）->
    点它 -> 对话框关闭，表单 checkbox 变 checked。
    对话框不出现也放行（可能该号已声明过），但最终 checked 必须为真。
    """
    box, _fr = await _find_visible(page, _ORIGINAL_CHECKBOX)
    if box is None:
        return False, "未找到可见的「声明原创」勾选框(选择器需校准 _ORIGINAL_CHECKBOX)"
    try:
        if await box.locator('input[type="checkbox"]').first.is_checked():
            return True, "已是勾选态"
    except Exception:
        pass
    try:
        await box.click(timeout=5000)
    except Exception as e:
        return False, f"点「声明原创」失败: {e!r}"
    await page.wait_for_timeout(2500)

    proto, _pf = await _find_visible(page, _ORIGINAL_PROTO_CHECKBOX)
    if proto is not None:
        try:
            await proto.click(timeout=5000)
            await page.wait_for_timeout(1200)
        except Exception as e:
            return False, f"勾原创协议失败: {e!r}"
        confirm, _cf = await _find_visible(page, _ORIGINAL_CONFIRM_BTN)
        if confirm is None:
            return False, "对话框里没找到「声明原创」按钮"
        try:
            if await confirm.is_disabled() or "disabled" in (
                    (await confirm.get_attribute("class")) or ""):
                return False, "「声明原创」按钮仍是 disabled(协议未勾上)"
            await confirm.click(timeout=5000)
        except Exception as e:
            return False, f"点对话框「声明原创」失败: {e!r}"
        await page.wait_for_timeout(2500)

    box2, _f2 = await _find_visible(page, _ORIGINAL_CHECKBOX)
    try:
        if box2 is not None and await box2.locator(
                'input[type="checkbox"]').first.is_checked():
            return True, "已勾选"
    except Exception:
        pass
    return False, "走完流程但 checkbox 仍非勾选态"


async def _mark_ai_generated(page) -> Tuple[bool, str]:
    """在「视频标注」下拉里选「含AI生成内容」。

    实测（2026-08-01 真号）：下拉共 7 项——无需标注 / 含AI生成内容 / 内容为虚构剧情，
    仅供娱乐 / 个人观点，仅供参考 / 内容包含营销广告 / 内容为自行拍摄 / 内容为转载。
    收起时选项在 DOM 里但不可见，必须先点开下拉；且要等原创对话框关掉，
    否则浮层会拦截 pointer events（报 "intercepts pointer events"）。
    """
    ctl, _fr = await _find_visible(page, _MARK_TAG_SELECT)
    if ctl is None:
        return False, "未找到「视频标注」下拉(选择器需校准 _MARK_TAG_SELECT)"
    try:
        if "含AI生成内容" in ((await ctl.inner_text()) or ""):
            return True, "已是「含AI生成内容」"
    except Exception:
        pass
    try:
        await ctl.click(timeout=5000)
    except Exception as e:
        return False, f"展开「视频标注」下拉失败: {e!r}"
    await page.wait_for_timeout(1800)

    ai, _af = await _find_visible(page, _AI_TAG)
    if ai is None:
        return False, "下拉已展开但没找到「含AI生成内容」选项"
    try:
        await ai.click(timeout=5000)
    except Exception as e:
        return False, f"点「含AI生成内容」失败: {e!r}"
    await page.wait_for_timeout(1500)

    # 校验：控件文字应从「选择视频标注」变成选中的标注名
    disp, _df = await _find_visible(page, _MARK_TAG_DISPLAY)
    try:
        txt = (await disp.inner_text()) if disp is not None else ""
    except Exception:
        txt = ""
    if "含AI生成内容" in txt:
        return True, "已选中「含AI生成内容」"
    return False, f"点了但控件仍显示「{txt.strip()[:30]}」，未确认选中"


async def _find_in_frames(page, selectors):
    """在主页面 + 所有子 frame(wujie iframe)里找第一个命中的定位器。
    Patchright 定位器默认穿透**开放** shadowRoot,但不穿 iframe,故需遍历 page.frames。
    返回 (locator, frame) 或 (None, None)。"""
    for frame in page.frames:                # page.frames[0] 即主 frame
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count():
                    return loc, frame
            except Exception:
                continue
    return None, None


async def _collect_diag(page, tag: str) -> str:
    """采集发布页真实 DOM 诊断,返回**紧凑单行摘要**(会拼进 UI 错误文案,你不用翻控制台),
    同时打到服务端日志。每个 frame 报:普通/shadow 里的 file input 数、upload 元素数、
    .post-view 数、按钮文案。"""
    parts = []
    try:
        for i, fr in enumerate(page.frames):
            try:
                info = await fr.evaluate("""() => {
                    const q = (s) => document.querySelectorAll(s).length;
                    let sf = 0;
                    const walk = (root) => root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) { sf += el.shadowRoot.querySelectorAll('input[type=file]').length; walk(el.shadowRoot); }
                    });
                    try { walk(document); } catch(e){}
                    const btns = [...document.querySelectorAll('button,[role=button],.weui-desktop-btn')]
                        .map(b => (b.innerText||'').trim()).filter(Boolean).slice(0, 8);
                    const host = (location.host||'') + (location.pathname||'');
                    return `${host} fi=${q('input[type=file]')} sf=${sf} up=${q('[class*=upload]')} pv=${q('.post-view')} btn=[${btns.join('/')}]`;
                }""")
                parts.append(f"f{i}:{info}")
            except Exception:
                parts.append(f"f{i}:eval_err")
    except Exception:
        pass
    summary = " | ".join(parts) or "无 frame 信息"
    log.warning("[channels_publish/%s] %s", tag, summary)
    return summary


async def publish_channels(mgr: BrowserManager, identity: Identity,
                           storage_state_json: str, media_type: str, title: str,
                           desc: str, media_paths: List[str], topics: str = "",
                           headed: bool = True, timeout_seconds: int = 180,
                           location: str = "",
                           declare_original: Optional[bool] = None,
                           ai_generated: Optional[bool] = None,
                           dry_run: bool = False,
                           ) -> Tuple[bool, str, str]:
    """发布一条视频号作品。返回 (ok, result_url, error)。
    location:可选,视频号位置 POI(best-effort,设不上不影响发布)。
    declare_original / ai_generated:None 表示用模块默认(DEFAULT_*，均为 True)。
      与 location 不同,这两项**勾不上就不发**——发出去了没标记撤不回来。
    dry_run:走完上传/填写/勾选,**停在点发表之前**返回,用于校准选择器而不真发。
    storage_state_json 仅校验用,实际登录态在该账号持久 profile 里。"""
    want_original = (DEFAULT_DECLARE_ORIGINAL if declare_original is None
                     else declare_original)
    want_ai = DEFAULT_AI_GENERATED if ai_generated is None else ai_generated
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]
    # 视频号正文:描述 + 话题(标题另填短标题框)
    body = ((desc or "")
            + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()[:1000]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()

    # ── 录制发布链路的完整请求，供「改造成纯接口调用」用（见 docs/channels-api-reverse.md）
    # 抓包时 body 会被截断，这里在真实发布过程中把完整 body 落盘，只记不改、不影响发布。
    api_log: List[dict] = []

    def _rec(req):
        u = req.url
        if "mmfinderassistant-bin/post/" not in u and "/applyuploaddfs" not in u \
                and "/completepartuploaddfs" not in u:
            return
        try:
            body = req.post_data or ""
        except Exception:
            body = ""
        api_log.append({
            "url": u, "method": req.method,
            "headers": {k: v for k, v in req.headers.items()
                        if k.lower() in ("authorization", "x-wechat-uin", "content-type")},
            "body": body[:20000],
        })

    page.on("request", _rec)
    ok, result_url, error = False, "", ""
    try:
        # 视频号发布入口就是 create 页;图文/视频靠点左侧导航切换(finderNewLifeCreateg 会 302 回 create)
        await page.goto(CREATE_URL_VIDEO, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(4000)
        if "login.html" in page.url or page.url.rstrip("/").endswith("/login"):
            return False, "", f"logged_out:视频号助手未登录(落到 {page.url})"

        # wujie 微前端挂载慢，死等 4 秒会把「还没渲染」误判成「找不到上传入口」
        # (2026-08-01 真号实测：轮询命中通常 3~9 秒，偶尔更久)
        for _ in range(30):
            probe, _pf0 = await _find_in_frames(page, _PAGE_READY)
            if probe is not None:
                break
            await page.wait_for_timeout(3000)
        else:
            diag = await _collect_diag(page, "page-not-ready")
            return False, "", f"发布页 90s 未挂载出主内容区。DOM诊断: {diag}"
        await page.wait_for_timeout(2000)

        # 残留草稿会弹「保存/不保存」挡住表单 —— 先清掉
        if await _click_in_frames(page, _DRAFT_DISCARD, timeout=2500):
            log.info("[channels_publish] 检测到草稿提示，已选「不保存」")
            await page.wait_for_timeout(2000)

        # 图文:点左侧「图文」→ 图文列表页 → 点「发表图文」→ 图文发布表单
        # (视频号 UI 在 micro/content iframe 里,按钮要跨 frame 点)
        if media_type == "images":
            await _click_in_frames(page, _IMAGE_NAV, timeout=4000)
            await page.wait_for_timeout(2000)
            if await _click_in_frames(page, _CREATE_IMAGE_BTN, timeout=5000):
                await page.wait_for_timeout(2500)
            else:
                diag = await _collect_diag(page, "no-create-image-btn")
                return False, "", f"未找到「发表图文」按钮,无法进图文发布页。DOM诊断: {diag}"

        want = files if media_type == "images" else files[:1]
        uploaded = False
        # 首选:直接给隐藏 <input type=file> 塞文件(不弹原生对话框,最稳)
        up, _fr = await _find_in_frames(page, _FILE_SEL)
        if up is not None:
            try:
                await up.set_input_files(want, timeout=20000)
                uploaded = True
            except Exception:
                uploaded = False
        # 兜底:input 是点击时按需创建 —— 用 expect_file_chooser 拦截原生文件框(关键!
        # 裸点上传区会弹 Windows「打开」对话框把 Patchright 卡死,必须这样接管)
        if not uploaded:
            try:
                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    if not await _click_in_frames(page, _UPLOAD_ZONE, timeout=4000):
                        raise RuntimeError("未点到上传区")
                chooser = await fc_info.value
                await chooser.set_files(want)
                uploaded = True
            except Exception:
                uploaded = False
        if not uploaded:
            diag = await _collect_diag(page, "upload-failed")
            return False, "", (f"上传失败(未找到可用上传入口/文件选择器)。DOM诊断: {diag}")

        # 等待上传/转码。⚠️ 原来固定等 8 秒，对新文件根本不够：转码没完成时「发表」
        # 按钮虽然是亮的，点下去却**静默无反应**（不跳转、不弹窗、不报错），
        # 2026-08-01 用一条 19MB 新视频连撞两次。唯一成功的那次是同一文件的第 6 次上传，
        # 服务端已有转码结果才立刻就绪——那是假象，别按它调参。
        # 判据：视频卡片渲染完成后会出现「删除」按钮，且「上传中/转码中」提示消失。
        if media_type == "video":
            for waited in range(0, 180, 5):
                await page.wait_for_timeout(5000)
                done = False
                for fr in page.frames:
                    try:
                        if not await fr.locator('button:has-text("删除")').first.count():
                            continue
                        busy = 0
                        for kw in ("上传中", "转码中", "处理中", "%"):
                            busy += await fr.get_by_text(kw, exact=False).count()
                        if busy == 0:
                            done = True
                            break
                    except Exception:
                        continue
                if done:
                    log.info("[channels_publish] 视频就绪（等了 %ds）", waited + 5)
                    break
            else:
                log.warning("[channels_publish] 180s 后仍未确认视频就绪，继续尝试")
            await page.wait_for_timeout(5000)      # 就绪后再缓冲一下
        else:
            await page.wait_for_timeout(4000)

        # 短标题：视频号要求**至少 6 个字**，不足会红字报错且「发表」按钮一直 disabled
        # (2026-08-01 真号实测：填「别急着定义」5 字就卡在这，之前只截断没管下限)。
        # 不足就用描述正文补齐——描述是现成的完整句子，比硬凑词自然。
        # 短标题是**选填**的：留空不校验；一旦填了就必须 ≥6 字且不含平台禁用符号，
        # 否则红字报错、「发表」按钮永久 disabled（2026-08-01 真号实测两种都撞过）。
        # 不足 6 字宁可不填，也不要拿描述硬凑——凑出来的是半截句子，读着是断的。
        short = _clean_short_title(title)[:16]
        if 0 < len(short) < 6:
            log.info("[channels_publish] 短标题「%s」不足 6 字，留空不填", short)
            short = ""
        if short:
            el, _tf = await _find_in_frames(page, _SHORT_TITLE_SEL)
            if el is not None:
                with suppress(Exception):
                    await el.fill(short[:16])
        if body:
            el, _df = await _find_in_frames(page, _DESC_SEL)
            if el is not None:
                try:
                    await el.click()
                    await page.keyboard.type(body, delay=20)
                except Exception:
                    pass
        # ⚠️⚠️ 这三步的**顺序不能改**：原创 → AI 标注 → 位置。
        # 2026-08-01 五次真号对照实测：
        #   位置 → 原创 → AI → 发表   ❌ 失败 3/3（点发表毫无反应，抓包证实**一条发布
        #                                请求都没发出**，不报错、不跳转、不弹窗）
        #   原创 → AI → 位置 → 发表   ✅ 成功 2/2
        # 原因是这三步各自都要弹浮层（原创弹对话框、AI 和位置各是一个下拉），
        # 把位置放前面，后面两层浮层叠上来，最后那次点击就被吃掉了。
        # 位置放最后，它的下拉收起时会把前面的残留一并清干净。
        # 原创声明 / AI 标注**必须在点发表之前**，且任一失败就中止不发。
        if want_original:
            ok_o, why_o = await _declare_original(page)
            log.info("[channels_publish] 声明原创: %s (%s)", ok_o, why_o)
            if not ok_o:
                diag = await _collect_diag(page, "declare-original-failed")
                return False, "", (f"要求声明原创但没勾上,已中止未发表: {why_o}。DOM诊断: {diag}")
        if want_ai:
            ok_a, why_a = await _mark_ai_generated(page)
            log.info("[channels_publish] 含AI生成内容: %s (%s)", ok_a, why_a)
            if not ok_a:
                diag = await _collect_diag(page, "ai-tag-failed")
                return False, "", (f"要求标注含AI生成内容但没点上,已中止未发表: {why_a}。DOM诊断: {diag}")

        # 位置放在最后（见上面顺序说明）。不传 location 时会清成「不显示位置」。
        await _set_location(page, location)
        await page.wait_for_timeout(1500)

        if dry_run:
            shot = str(Path.cwd() / "logs" / "channels_dryrun.png")
            try:
                await page.screenshot(path=shot, full_page=True)
            except Exception:
                shot = "(截图失败)"
            pub_probe, _ = await _find_in_frames(page, _PUBLISH_BTN)
            return True, shot, ("DRY-RUN 已停在点发表之前｜"
                                f"发表按钮{'可定位' if pub_probe is not None else '未找到'}")

        # 用 _find_visible 而非 _find_in_frames：页面里有不止一个匹配 _PUBLISH_BTN 的
        # 节点，不判可见性可能点到隐藏的那个。
        #
        # ⚠️⚠️ 2026-08-01 血的教训：那次点完发表 URL 一直停在 post/create，函数按
        # 「90 秒没跳转」判定失败并返回 ok=False；紧接着查作品列表也确实没新增，于是
        # 判定「没发出去」→ 重发了一次 → **同一条内容在视频号上发了两条**。
        # 事后核对：第一条的 create_time 是点发表后约 3.5 分钟才落地的。
        # 结论：**视频号发表是异步的，页面不跳转 ≠ 没发成功，即时查列表也可能查不到。**
        # 所以下面返回 ok=False 时，调用方**绝不能直接重发**，必须先隔几分钟复查作品
        # 列表确认没有这条，再决定是否重试（见函数末尾 no-success 分支的说明）。
        pub, _pf = await _find_visible(page, _PUBLISH_BTN + ['button:has-text("发表")'])
        if pub is None:
            diag = await _collect_diag(page, "no-publish-btn")
            return False, "", (f"上传/填写已完成但未找到**可见的**发表按钮。DOM诊断: {diag}")
        # 点发表前后各截一张：失败时只有文字诊断根本看不出画面上发生了什么
        # （比如弹了确认框、报了红字、按钮其实是禁用态）
        shots = Path.cwd() / "logs"
        try:
            shots.mkdir(exist_ok=True)
            await page.screenshot(path=str(shots / "publish_1_before.png"), full_page=True)
        except Exception:
            pass
        try:
            btn_cls = (await pub.get_attribute("class")) or ""
            btn_txt = ((await pub.inner_text()) or "").strip()
            log.info("[channels_publish] 即将点击发表: text=%r class=%r disabled=%s",
                     btn_txt, btn_cls, await pub.is_disabled())
        except Exception:
            pass
        try:
            await pub.click(timeout=8000)
        except Exception as e:
            return False, "", f"点发表失败: {e!r}"
        await page.wait_for_timeout(5000)

        # ⚠️ Playwright 的 click 是**坐标点击**，会被 pointer-events:none 的透明层
        # 静默吃掉（它不报错，因为这类层不算「遮挡」）。2026-08-01 抓包实证：点完只有
        # 两条埋点上报、**没有任何发布接口调用**，页面停在 post/create 毫无反应。
        # 这里用 JS 原生 click 兜底，直接在元素上派发事件、绕过坐标命中。
        # 只在「页面还停在 create 页」时补这一下——真发出去了会跳 post/list，不会重复。
        if "post/create" in page.url.lower():
            try:
                await pub.evaluate("el => el.click()")
                log.info("[channels_publish] 坐标点击无效，已用 JS click 兜底")
                await page.wait_for_timeout(5000)
            except Exception as e:
                log.warning("[channels_publish] JS click 兜底失败: %r", e)
        try:
            await page.screenshot(path=str(shots / "publish_2_after.png"), full_page=True)
            log.info("[channels_publish] 点击后 URL=%s", page.url)
        except Exception:
            pass

        # 等成功:视频号发表后会**跳到「图文/视频管理」列表页**(URL 含 PostList),
        # 或短暂弹「发表成功」toast。以跳列表页为主判据(实测 finderNewLifePostList)。
        for _ in range(int(timeout_seconds / 2)):
            url_l = page.url.lower()
            # finderNewLifePostList / post/list 等管理列表页 -> 发表成功后的落点
            if "postlist" in url_l or "/post/list" in url_l:
                ok = True
                break
            # toast 可能在主页面或 micro/content iframe 里
            for fr in page.frames:
                try:
                    if any([await fr.get_by_text(t, exact=False).count() for t in _OK_TEXTS]):
                        ok = True
                        break
                except Exception:
                    pass
            if ok:
                break
            await page.wait_for_timeout(2000)
        result_url = page.url if ok else ""
        if not ok:
            diag = await _collect_diag(page, "no-success")
            # ⚠️ 这个分支**不等于发布失败**。视频号发表是异步的：2026-08-01 实测有一条
            # 在点发表约 3.5 分钟后才出现在作品列表里，而此处早已按「没跳转」返回失败，
            # 直接重发就造成了同一内容重复发两条。
            try:
                await page.screenshot(path=str(Path.cwd() / "logs" / "publish_3_timeout.png"),
                                      full_page=True)
            except Exception:
                pass
            error = ("已点发表但未确认成功——**可能仍会稍后发布成功**(视频号发表异步，实测有过"
                     "3.5 分钟延迟)。⚠️ 请勿直接重发：先隔 5 分钟同步作品列表确认这条不在，"
                     "再决定重试。也可能是视频号要求封面/实名/过脸验证，请到助手确认。"
                     f"当前页: {page.url}; DOM诊断: {diag}")
    except Exception as e:
        error = f"发布异常: {e!r}"
    finally:
        # 落盘录到的接口调用（含完整 body / authorization），供接口化改造分析
        try:
            if api_log:
                out = Path.cwd() / "logs" / "channels_api_capture.json"
                out.parent.mkdir(exist_ok=True)
                out.write_text(json.dumps(api_log, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                log.info("[channels_publish] 已录 %d 条接口调用 -> %s", len(api_log), out)
        except Exception:
            pass
        with suppress(Exception):
            await ctx.close()
    return ok, result_url, error
