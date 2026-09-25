"""运营日报 flow（蓝图 §5.5/§6.4）：纯编排、CH 未启用时的降级、注册表接线。

hermetic：不碰 Prefect engine（编排注入普通函数），也不连 ClickHouse（未启用即短路）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core import clickhouse
from app.core.messaging import SUB_JOBS
from app.core.task_registry import ensure_tasks_registered, handlers_for
from app.flows import ops_daily_body
from app.flows.ops_daily import orchestrate_ops_daily


async def test_orchestrate_passes_window_and_returns_report() -> None:
    """编排只做校验与透传；收集逻辑由注入方提供（生产=Prefect task，测试=普通函数）。"""
    seen: dict[str, Any] = {}

    async def _fake_collect(*, days: int) -> dict[str, Any]:
        seen["days"] = days
        return {"window_days": days, "totals": {"event_failures": 0}}

    result = await orchestrate_ops_daily(days=7, collect=_fake_collect)
    assert seen["days"] == 7
    assert result["window_days"] == 7


@pytest.mark.parametrize("days", [0, -1])
async def test_orchestrate_rejects_non_positive_window(days: int) -> None:
    """非正窗口 → 直接报错，而不是查出「空日报」让运维以为没数据。"""

    async def _never_called(*, days: int) -> dict[str, Any]:
        raise AssertionError("不应在窗口非法时调用收集")

    with pytest.raises(ValueError):
        await orchestrate_ops_daily(days=days, collect=_never_called)


async def test_ch_routes_degrade_to_empty_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse 未启用 → 两路返回空 dict（日报缺一路仍比整体失败有用）。"""
    monkeypatch.setattr(clickhouse, "is_enabled", lambda: False)
    assert await ops_daily_body.count_event_failures_by_day(3) == {}
    assert await ops_daily_body.count_audit_logs_by_day(3) == {}


async def test_ch_route_swallows_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CH 查询抛错也按该路置空处理，不把日报整体打挂。"""
    monkeypatch.setattr(clickhouse, "is_enabled", lambda: True)

    async def _boom() -> Any:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(clickhouse, "get_client", _boom)
    assert await ops_daily_body.count_event_failures_by_day(3) == {}


def test_ops_daily_task_registered_and_cron_wired() -> None:
    """handler 注册在 jobs 订阅上（cron 触发消息才能被 worker 分发到它）。"""
    ensure_tasks_registered()
    assert "run_ops_daily" in handlers_for(SUB_JOBS.name)
