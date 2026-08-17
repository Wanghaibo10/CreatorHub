"""监控引擎。对应逆向 engine.MonitorEngine + ContentChecker。
后台循环:到点的目标 -> 真实浏览器抓新作品 -> 入库 -> 下载。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import select

from application.browser import BrowserManager
from moss.core.config import Config
from moss.common.db import get_session
from application.douyin import parse_aweme
from application.douyin.extract import Aweme
# 图文平台(纯 HTTP,不开浏览器):凭证是账号 Cookie,不吃 browser/identity/state
from moss.model import CommentTask, PublishTask, AccountActionTask, KeywordCollectionJob
from moss.core.risk import RiskController
from application.engine.downloader import Downloader
from application.engine.collection import KeywordCollector

_BROWSER_SUBMIT_MARKER = "write_submitted:browser"

# MonitorEngine 按职责拆成 7 个 Mixin(2026-08-17):方法仍是同一实例的方法,
# self 状态共享不变。加新能力去对应文件,别再往本文件堆方法。
from application.engine.gates import GateOps
from application.engine.scanning import ScanOps
from application.engine.watches import WatchOps
from application.engine.publishing import PublishOps
from application.engine.commenting import CommentOps
from application.engine.actions import ActionOps
from application.engine.collections import CollectionOps

from application.engine._helpers import (MAX_AUTO_RETRY, _TZ_COUNTRY, log, _loads, _loads_list, _danmaku_matches,
                                         _select_douyin_awemes, _douyin_scan_since,
                                         _round_robin_by_account)

# 账号时区 -> 期望出口国家(ISO2)。仅列常见,匹配不到则跳过地区校验。














class MonitorEngine(GateOps, ScanOps, WatchOps, PublishOps, CommentOps,
                    ActionOps, CollectionOps):
    def __init__(self, cfg: Config, browser: BrowserManager):
        self.cfg = cfg
        self.browser = browser
        self.downloader = Downloader(
            cfg.engine.media_dir, cfg.engine.user_agent,
            cfg.engine.download_timeout_seconds,
        )
        self.keyword_collector = KeywordCollector(cfg, browser, self.downloader)
        self._sem = asyncio.Semaphore(cfg.engine.worker_pool_size)
        # 限制并发抓取的目标数(多个浏览器上下文并行,但不无限开)
        self._scan_sem = asyncio.Semaphore(max(1, cfg.engine.scan_concurrency))
        # 同一时刻最多并发活跃的账号数(错峰,降低"多号同时活跃"特征)
        self._active_sem = asyncio.Semaphore(max(1, cfg.engine.active_accounts))
        self._inflight: set = set()           # 正在抓取的目标,避免同目标并发
        self._publish_sem = asyncio.Semaphore(1)   # 发布串行(有头浏览器,一次一个)
        self._publishing: set[int] = set()
        self._commenting: set[int] = set()         # 正在执行的评论任务 id
        self._actioning: set[int] = set()           # 正在执行的写操作任务 id
        self._collection_tasks: dict[int, asyncio.Task] = {}  # 一次性关键词采集
        self._last_acct_check = time.time()   # 上次账号体检时间
        self._geo_checked: dict = {}          # account_id -> 已校验过地区的代理(避免重复探测)
        self.risk = RiskController(cfg)
        self._last_risk_prune_day = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            log.info("监控引擎已启动")

    def recover_interrupted_tasks(self, *, now: datetime | None = None,
                                  delay_seconds: int = 300) -> int:
        """Return crash-left transient write states to their durable queues."""
        scheduled_at = (now or datetime.utcnow()) + timedelta(
            seconds=max(1, delay_seconds))
        recovered = 0
        with get_session() as s:
            for model, transient in (
                    (CommentTask, "doing"),
                    (AccountActionTask, "doing"),
                    (PublishTask, "publishing")):
                rows = s.exec(select(model).where(model.status == transient)).all()
                for row in rows:
                    submitted = (
                        getattr(row, "platform", "") == "xhs"
                        and str(getattr(row, "error", "") or "")
                        .startswith(_BROWSER_SUBMIT_MARKER)
                    )
                    if submitted:
                        row.status = "uncertain"
                        row.scheduled_at = None
                        if hasattr(row, "done_at"):
                            row.done_at = None
                        row.error = (
                            "服务重启前浏览器已进入提交边界，结果需到平台核对；"
                            "任务不会自动重试")
                    else:
                        row.status = "pending"
                        row.scheduled_at = scheduled_at
                        row.error = "服务重启后已恢复到待执行队列"
                    s.add(row)
                    recovered += 1
            for job in s.exec(
                    select(KeywordCollectionJob)
                    .where(KeywordCollectionJob.status == "running")).all():
                if job.cancel_requested:
                    job.status = "canceled"
                    job.current_step = "已取消"
                    job.finished_at = now or datetime.utcnow()
                else:
                    job.status = "pending"
                    job.current_step = "服务重启后等待继续"
                    job.started_at = None
                    job.finished_at = None
                s.add(job)
                recovered += 1
            if recovered:
                s.commit()
        return recovered

    @staticmethod
    def _mark_browser_submit(model, task_id: int) -> None:
        """Durably mark the conservative no-retry boundary before one click."""
        with get_session() as s:
            row = s.get(model, task_id)
            if row is None:
                raise RuntimeError("待提交任务已不存在")
            row.error = _BROWSER_SUBMIT_MARKER
            s.add(row)
            s.commit()

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        tasks = list(self._collection_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._collection_tasks.clear()

    # ── 账号隔离调度 ──


    async def _collect_idle_browser_sessions(self, now: float | None = None) -> int:
        """Reuse the main scheduler to close idle owned XHS Chrome sessions."""
        collector = getattr(self.browser, "collect_idle_cdp", None)
        if not callable(collector):
            return 0
        try:
            return int(await collector(now=now))
        except Exception:
            log.exception("idle XHS CDP collection failed")
            return 0

    async def _loop(self):
        while self._running:
            try:
                sampled_at = datetime.utcnow()
                sampled_epoch = time.time()
                self._prune_risk_events_if_due(sampled_at)
                await self._collect_idle_browser_sessions(sampled_epoch)
                await self._scan_once()
                await self._scan_comment_watches()
                await self._scan_danmaku_watches()
                await self._retry_failed()
                await self._check_accounts()
                await self._check_work_health()
                await self._process_publish()
                await self._process_comment_rules()
                await self._process_comment_tasks()
                await self._process_action_tasks()
                await self._process_collection_jobs()
            except Exception as e:
                log.exception("scan loop error: %s", e)
            await asyncio.sleep(15)


    # ── 账号登录态体检 + 闲置保活 ──

    # ── 本账号作品健康监控(B5)+ 数据快照(B4)──

    # 视为「异常/受限」的状态关键词(命中即告警)
    _BAD_STATUS = ("违规", "删除", "下架", "不适宜", "限流", "私密", "仅自己", "审核不")


    # ── 快手:创作者作品监控(浏览器拦截 GraphQL,与抖音同范式)──


    # ── 小红书:创作者笔记 / 关键词 监控 ──

    # ── 独立弹幕监控(DanmakuWatch)──


    # ── 独立评论监控(CommentWatch)──


    # ── 快手评论监控(浏览器拦截 GraphQL)──


    # ── 小红书评论监控(签名直连 API)──


    # ── 发布(小红书创作平台)+ 跨平台转发 ──


    # ── 活跃时段(夜间静默)──

    # ── 自动评论:规则生成任务 + 任务执行 ──


    # ── 本账号写操作队列(取关/回关/发私信)──


    # ── 失败重试 ──


