"""
ClickHouse 分析导出的纯体层（M5 7.2.6，**无 Prefect 依赖**）。
被两条路径共用：
- ``app/flows/analytics.py`` 的 Prefect task（生产，带重试）；
- ``auth/tasks.py`` 的 cron 回落直调（``LKM_PREFECT_ENABLED=false`` 或触发失败）。
audit 那一路经 ``core.ports.audit`` 取 auth 能力（core 不 import auth）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from core import clickhouse
from core.config import settings


def _window(window: int | None) -> int:
    return window if window is not None else settings.clickhouse_export_window


async def run_event_failures_export(*, window: int | None = None) -> int:
    """业务库 event_failures → CH；未启用 CH 视为 no-op(0)，不报错。"""
    from core.db.event_failure_export import export_event_failures
    from core.db.session import new_worker_session as new_session

    if not clickhouse.is_enabled():
        return 0
    client = await clickhouse.get_client()
    db = await new_session()
    try:
        return await export_event_failures(db, client, window=_window(window))
    finally:
        await db.close()


async def run_audit_logs_export(*, window: int | None = None) -> int:
    """auth 库 audit_logs → CH；未启用 CH 视为 no-op(0)，不报错。"""
    from core.ports.audit import export_audit_logs, new_auth_session

    if not clickhouse.is_enabled():
        return 0
    client = await clickhouse.get_client()
    db = await new_auth_session()
    try:
        return await export_audit_logs(db, client, window=_window(window))
    finally:
        await db.close()


async def run_analytics_export(*, window: int | None = None) -> dict[str, Any]:
    """两路各导一次；一路失败时仍让另一路完成，并把错误交给 worker。"""
    failures, audits = await asyncio.gather(
        run_event_failures_export(window=window),
        run_audit_logs_export(window=window),
        return_exceptions=True,
    )
    if isinstance(failures, BaseException):
        raise failures
    if isinstance(audits, BaseException):
        raise audits
    return {"event_failures": failures, "audit_logs": audits}
