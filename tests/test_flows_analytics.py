"""analytics Prefect flow 纯编排测试（M5 7.2.6）。

编排控制流 ``orchestrate_analytics_export`` 与 Prefect 解耦：注入普通实现函数直接跑，不触碰
Prefect engine。只断言编排分派（window 透传、两路各一次）。
"""

from __future__ import annotations

from typing import Any

from app.flows.analytics import analytics_export_flow, orchestrate_analytics_export


async def should_orchestrate_both_routes_once() -> None:
    calls: dict[str, Any] = {}

    async def export_failures(*, window: int) -> int:
        calls["failures"] = window
        return 3

    async def export_audits(*, window: int) -> int:
        calls["audits"] = window
        return 2

    result = await orchestrate_analytics_export(
        window=7,
        export_failures=export_failures,
        export_audits=export_audits,
    )

    assert result == {"event_failures": 3, "audit_logs": 2}
    assert calls == {"failures": 7, "audits": 7}  # window 透传给两路各一次


def should_flow_metadata() -> None:
    assert analytics_export_flow.name == "analytics-clickhouse-export"
    assert analytics_export_flow.retries == 1
