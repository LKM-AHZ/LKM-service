"""ClickHouse 分析导出的纯体层（M5 7.2.6，**无 Prefect 依赖**）。

被两条路径共用：
- ``app/flows/analytics.py`` 的 Prefect task（生产，带重试）；
- ``app/modules/auth/tasks.py`` 的 cron 回落直调（``LKM_PREFECT_ENABLED=false`` 或触发失败）。

刻意不 import prefect：保证回落路径与「默认关」场景零 Prefect 依赖、worker 冷启动不被拖累。
负责开各 realm 会话并调用 owner 侧导出入口（业务库 event_failures / auth 库 audit_logs），
不复制业务 SQL。
"""

from __future__ import annotations

from typing import Any

from app.core import clickhouse
from app.core.config import settings


def _window(window: int | None) -> int:
    return window if window is not None else settings.clickhouse_export_window


async def run_event_failures_export(*, window: int | None = None) -> int:
    """业务库 event_failures → CH；未启用 CH 视为 no-op(0)，不报错。"""
    from app.db.event_failure_export import export_event_failures
    from app.db.session import new_session

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
    from app.db.auth_session import get_auth_session
    from app.modules.auth.audit_export import export_audit_logs

    if not clickhouse.is_enabled():
        return 0
    client = await clickhouse.get_client()
    db = await get_auth_session()
    try:
        return await export_audit_logs(db, client, window=_window(window))
    finally:
        await db.close()


async def run_analytics_export(*, window: int | None = None) -> dict[str, Any]:
    """两路各导一次（回落直调入口）。"""
    failures = await run_event_failures_export(window=window)
    audits = await run_audit_logs_export(window=window)
    return {"event_failures": failures, "audit_logs": audits}
