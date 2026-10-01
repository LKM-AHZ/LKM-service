"""analytics Prefect flow 纯编排测试。

编排控制流 ``orchestrate_analytics_export`` 与 Prefect 解耦：注入普通实现函数直接跑，不触碰
Prefect engine。只断言编排分派（window 透传、两路各一次）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.flows.analytics import (
    _export_audits,
    _export_failures,
    analytics_export_flow,
    orchestrate_analytics_export,
)
from core.flows import analytics_body


async def should_call_moved_export_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []

    async def failures(*, window: int) -> int:
        seen.append(("failures", window))
        return 3

    async def audits(*, window: int) -> int:
        seen.append(("audits", window))
        return 2

    monkeypatch.setattr(analytics_body, "run_event_failures_export", failures)
    monkeypatch.setattr(analytics_body, "run_audit_logs_export", audits)

    assert await _export_failures(window=7) == 3
    assert await _export_audits(window=7) == 2
    assert seen == [("failures", 7), ("audits", 7)]


async def should_export_audits_even_when_failures_route_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def failures(*, window: int | None = None) -> int:
        seen.append("failures")
        raise RuntimeError("event export failed")

    async def audits(*, window: int | None = None) -> int:
        seen.append("audits")
        return 2

    monkeypatch.setattr(analytics_body, "run_event_failures_export", failures)
    monkeypatch.setattr(analytics_body, "run_audit_logs_export", audits)

    with pytest.raises(RuntimeError, match="event export failed"):
        await analytics_body.run_analytics_export(window=7)
    assert sorted(seen) == ["audits", "failures"]


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
