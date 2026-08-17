"""账号绑定读操作的统一入口(过引擎风控闸门)。

从 main.py 抽出(2026-08-17 模块化):accounts 与 publish 域共用。
"""
from __future__ import annotations

from fastapi import HTTPException

from moss.core.risk import OperationKind
from moss.core.runtime import rt


async def _run_account_read(account_id: int, kind: OperationKind, key: str,
                            operation, *, empty_result,
                            unexpected_detail: str = ""):
    """Run one account-bound API read through the engine's unified gates."""
    if rt.engine is None:
        raise HTTPException(503, "引擎未就绪")
    guarded_empty = empty_result
    if unexpected_detail and isinstance(empty_result, dict):
        guarded_empty = dict(empty_result)
        guarded_empty["_guard_error"] = True
    payload, error = await rt.engine.guarded_read_pair(
        account_id, kind, key, operation, empty_result=guarded_empty)
    guard_error = False
    if isinstance(payload, dict):
        payload = dict(payload)
        guard_error = bool(payload.pop("_guard_error", False))
    error_text = str(error or "")
    if error_text.startswith("risk_deferred:"):
        deferred = ({key: value for key, value in empty_result.items()
                     if not str(key).startswith("_")}
                    if isinstance(empty_result, dict) else {})
        deferred.update({
            "skipped": True,
            "reason": error_text.split(":", 1)[-1],
        })
        return None, deferred
    if unexpected_detail and guard_error and error:
        raise HTTPException(500, unexpected_detail)
    return (payload, None) if not error else (payload, error_text)
