"""FastAPI 入口:只做装配(日志/DB/浏览器/引擎的启动顺序)与 router 挂载。

顶层三包(对齐 mosshotel):app/=Web 层(api/ service/ static/),
application/=平台业务(一平台一包 + browser/ engine/),
moss/=基础设施(common/ model/ core/)。运行时共享状态在 moss/core/runtime.py。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from application.browser import BrowserManager
from moss.core.config import get_config
from moss.common.db import init_db
from application.engine import MonitorEngine
from moss.common.logging_setup import get_logger, setup_logging
from app.service.profiles import migrate_identities, seed_proxy_pool
from app.api import (account_data as account_data_router,
                     accounts as accounts_router,
                     collections as collections_router,
                     comment_auto as comment_auto_router,
                     contents as contents_router,
                     login as login_router,
                     monitors as monitors_router,
                     notifications as notifications_router,
                     proxies as proxies_router,
                     publish as publish_router,
                     reports as reports_router,
                     settings as settings_router,
                     share_download as share_download_router,
                     watches as watches_router)
from moss.core.runtime import rt
from app.service.browser_windows import _persist_native_ua
from app.service.share_download import _backfill_share_download_history
from app.service.watch_data import _backfill_danmaku_records

log = get_logger("api")
cfg = get_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 日志必须最先配:在此之前所有 logger.info 都会被丢弃(root 零 handler)
    setup_logging()
    init_db(cfg.db_path)
    try:
        repaired = _backfill_danmaku_records()
        if repaired:
            log.info(f"已补齐 {repaired} 条弹幕的时间/用户字段")
    except Exception as e:
        log.warning(f"弹幕存量字段补齐失败（不影响启动）: {e!r}")
    try:
        restored = _backfill_share_download_history()
        if restored:
            log.info(f"已从本地下载目录补录 {restored} 条链接下载历史")
    except Exception as e:
        log.warning(f"链接下载历史补录失败（不影响启动）: {e!r}")
    # config.yaml 里配的 proxies 导入数据库代理池(之后统一在页面管理)
    try:
        seeded = seed_proxy_pool(cfg)
        if seeded:
            log.info(f"已从 config.yaml 导入 {seeded} 条代理到代理池")
    except Exception as e:
        log.warning(f"代理池导入失败(不影响启动): {e!r}")
    # 存量账号补齐设备/网络画像(profile_dir / UA / 指纹 / 代理),防多账号关联
    try:
        n = migrate_identities(cfg)
        if n:
            log.info(f"已为 {n} 个存量账号补齐画像(profile/UA/指纹/代理)")
    except Exception as e:
        log.warning(f"账号画像迁移失败(不影响启动): {e!r}")
    # 运行时状态统一放 rt(app/runtime.py):路由分散在 routers/ 各模块,
    # 模块级全局会被拆出去的模块拿到 None 快照,这里是唯一赋值点。
    rt.browser = BrowserManager(
        cfg.engine.user_agent, cfg.engine.profiles_dir,
        cfg.engine.max_live_contexts, native_ua_callback=_persist_native_ua,
        xhs_browser_mode=cfg.engine.xhs_browser_mode,
        xhs_cdp_idle_seconds=cfg.engine.xhs_cdp_idle_seconds,
        native_write_gate_enabled=cfg.engine.native_write_gate_enabled,
        native_write_require_system_chrome=cfg.engine.native_write_require_system_chrome,
        native_write_require_verified_proxy=cfg.engine.native_write_require_verified_proxy,
        native_write_proxy_max_age_seconds=cfg.engine.native_write_proxy_max_age_seconds,
        browser_exit_probe_url=cfg.engine.browser_exit_probe_url)
    await rt.browser.start()
    rt.engine = MonitorEngine(cfg, rt.browser)
    startup_now = datetime.utcnow()
    pruned_risk_events = rt.engine._prune_risk_events_if_due(startup_now)
    if pruned_risk_events:
        log.info(f"已清理 {pruned_risk_events} 条过期风控事件")
    recovered = rt.engine.recover_interrupted_tasks()
    if recovered:
        log.info(f"已恢复 {recovered} 条中断的写任务")
    rt.engine.start()
    from application.engine.im_receiver import ImReceiverManager
    rt.im_receiver = ImReceiverManager(rt.browser)
    yield
    if rt.im_receiver:
        await rt.im_receiver.stop_all()
    if rt.engine:
        await rt.engine.stop()
    if rt.browser:
        await rt.browser.stop()


app = FastAPI(title="CreatorHub", lifespan=lifespan)
WEB_DIR = Path(__file__).parent / "static"

for _r in (login_router, accounts_router, account_data_router, proxies_router,
           settings_router, notifications_router, comment_auto_router,
           reports_router, monitors_router, contents_router,
           share_download_router, collections_router, publish_router,
           watches_router):
    app.include_router(_r.router)


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # 给 app.js 带上基于 mtime 的版本号,前端改动后自动击穿浏览器缓存(免手动强刷)
    try:
        ver = int((WEB_DIR / "app.js").stat().st_mtime)
        html = html.replace("/static/app.js", f"/static/app.js?v={ver}")
    except Exception:
        pass
    # 首页(含内联 CSS)禁缓存:否则 webview 缓存旧 HTML,改了样式也不生效
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}
