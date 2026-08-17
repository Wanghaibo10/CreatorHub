"""关键词批量采集任务。

MonitorEngine 的 Mixin(2026-08-17 从 monitor.py 拆出):方法仍属同一个引擎
实例,self 状态共享不变;拆分只为按职责分文件。
"""
from __future__ import annotations

from __future__ import annotations
import asyncio
from datetime import datetime
from sqlmodel import select
from moss.common.db import get_session
from moss.model import DouyinAccount, KeywordCollectionJob
from moss.core.risk import classify_platform_error, OperationKind, RiskCategory


class CollectionOps:
    def enqueue_collection_job(self, job_id: int) -> bool:
        """立即把关键词任务交给后台执行；同一时刻仅跑一个批量任务。"""
        if job_id in self._collection_tasks:
            return False
        if len(self._collection_tasks) >= 1:
            return False
        task = asyncio.create_task(self.run_collection_job(job_id))
        self._collection_tasks[job_id] = task

        def done(completed: asyncio.Task, jid: int = job_id):
            self._collection_tasks.pop(jid, None)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done)
        return True

    async def _process_collection_jobs(self) -> None:
        if self._collection_tasks:
            return
        with get_session() as s:
            job = s.exec(
                select(KeywordCollectionJob)
                .where(KeywordCollectionJob.status == "pending")
                .where(KeywordCollectionJob.cancel_requested == False)  # noqa:E712
                .order_by(KeywordCollectionJob.created_at)
            ).first()
        if job:
            self.enqueue_collection_job(job.id)

    async def run_collection_job(self, job_id: int) -> dict:
        """执行一个持久化关键词任务，并维护可恢复的状态机。"""
        with get_session() as s:
            job = s.get(KeywordCollectionJob, job_id)
            if not job:
                return {"ok": False, "error": "任务不存在"}
            if job.cancel_requested or job.status == "canceled":
                job.status = "canceled"
                job.current_step = "已取消"
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                return {"ok": True, "canceled": True}
            account = s.get(DouyinAccount, job.account_id)
            if (not account or account.platform != job.platform
                    or account.status != "active" or not account.storage_state):
                job.status = "failed"
                job.current_step = "执行失败"
                job.error = "所选账号不存在、登录态失效或平台不匹配"
                job.error_count += 1
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                return {"ok": False, "error": job.error}
            account_id = account.id

        decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
        if not decision.allowed:
            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if job and job.status == "pending":
                    job.current_step = "等待账号读取冷却"
                    s.add(job); s.commit()
            return {"ok": True, "deferred": True, "reason": decision.reason}

        try:
            async with self._operation_guard(account_id, OperationKind.READ_HEAVY):
                decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
                if not decision.allowed:
                    return {"ok": True, "deferred": True, "reason": decision.reason}
                with get_session() as s:
                    job = s.get(KeywordCollectionJob, job_id)
                    if not job:
                        return {"ok": False, "error": "任务不存在"}
                    job.status = "running"
                    job.current_step = "准备搜索"
                    job.started_at = job.started_at or datetime.utcnow()
                    job.finished_at = None
                    s.add(job); s.commit()
                    account = s.get(DouyinAccount, account_id)
                result = await self.keyword_collector.run(job_id, account)

            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if not job:
                    return {"ok": False, "error": "任务不存在"}
                if result.get("canceled") or job.cancel_requested:
                    job.status = "canceled"
                    job.current_step = "已取消"
                elif job.error_count and job.content_count:
                    job.status = "partial"
                    job.current_step = "完成，部分内容有错误"
                elif job.error_count and not job.content_count:
                    job.status = "failed"
                    job.current_step = "执行失败"
                else:
                    job.status = "done"
                    job.current_step = "已完成"
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                status = job.status
                job_error = job.error or ""
            category, _ = classify_platform_error(job_error)
            if job_error and category in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                # 即使已经采到部分结果，验证码/登录态/网络异常也属于本次读取失败；
                # 不能再记 success，否则会立即放行下一次重读并覆盖风险信号。
                self.risk.record_failure(
                    account_id, OperationKind.READ_HEAVY, job_error)
            elif status in {"done", "partial"}:
                self.risk.record_success(account_id, OperationKind.READ_HEAVY)
                self._stamp_active(account_id)
            else:
                self.risk.record_failure(account_id, OperationKind.READ_HEAVY,
                                         "关键词采集未取得结果")
            return {"ok": status in {"done", "partial"}, "status": status, **result}
        except asyncio.CancelledError:
            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if job and job.status == "running":
                    if job.cancel_requested:
                        job.status = "canceled"
                        job.current_step = "已取消"
                        job.finished_at = datetime.utcnow()
                    else:
                        job.status = "pending"
                        job.current_step = "服务停止，等待恢复"
                    s.add(job); s.commit()
            raise
        except Exception as exc:
            self.risk.record_failure(account_id, OperationKind.READ_HEAVY, exc)
            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if job:
                    job.error_count += 1
                    job.error = (job.error + "\n" if job.error else "") + str(exc)[:500]
                    job.status = "partial" if job.content_count else "failed"
                    job.current_step = "异常中止"
                    job.finished_at = datetime.utcnow()
                    s.add(job); s.commit()
            return {"ok": False, "error": str(exc)}
