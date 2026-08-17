"""报表导出的共用辅助(时间窗口、xlsx 下载响应、筛选文案)。

从 main.py 抽出(2026-08-17 模块化):8 个 /api/reports/*.xlsx 路由与
关键词采集导出共用。xlsx 的实际构建在 app/reporting.py。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse


def _report_bounds(
    start_date: date | None,
    end_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    start = datetime.combine(start_date, time.min) if start_date else None
    end = (
        datetime.combine(end_date + timedelta(days=1), time.min)
        if end_date else None
    )
    return start, end


def _report_window(stmt, model, start: datetime | None, end: datetime | None):
    if start:
        stmt = stmt.where(model.created_at >= start)
    if end:
        stmt = stmt.where(model.created_at < end)
    return stmt


def _report_download(payload: bytes, prefix: str):
    from app.service.reporting import REPORT_MIME

    filename = f"creatorhub_{prefix}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return StreamingResponse(
        iter([payload]),
        media_type=REPORT_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


def _report_filter_pairs(pairs: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [(label, value if value not in (None, "") else "全部")
            for label, value in pairs]
