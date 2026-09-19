"""analytics cron 触发测试（M5 7.2.6）。

复刻 test_prefect_trigger 的替身范式（monkeypatch run_deployment + 纯体层导出函数计数），
覆盖「开关关 → 直调 / 开关开且配了 analytics deployment → 触发 / 触发失败 → 回落直调 /
配了开关但未配 deployment → 直调」四条语义。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.flows import analytics_body
from auth import tasks as auth_tasks


@pytest.fixture
def spy_direct(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """替身直调：记录调用次数，返回空结果（不触网、不建会话）。"""
    calls: list[dict[str, Any]] = []

    async def fake_export(*, window: int | None = None) -> dict[str, Any]:
        calls.append({"window": window})
        return {"event_failures": 0, "audit_logs": 0}

    monkeypatch.setattr(analytics_body, "run_analytics_export", fake_export)
    return calls


def _no_trigger() -> None:
    raise AssertionError("不应触发 Prefect")


async def should_direct_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, spy_direct: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings, "prefect_enabled", False)
    monkeypatch.setattr("prefect.deployments.run_deployment", _no_trigger)

    await auth_tasks.export_analytics_clickhouse()

    assert len(spy_direct) == 1


async def should_direct_when_deployment_missing(
    monkeypatch: pytest.MonkeyPatch, spy_direct: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings, "prefect_enabled", True)
    monkeypatch.setattr(settings, "prefect_analytics_deployment", "")
    monkeypatch.setattr("prefect.deployments.run_deployment", _no_trigger)

    await auth_tasks.export_analytics_clickhouse()

    assert len(spy_direct) == 1


async def should_trigger_deployment_when_enabled(
    monkeypatch: pytest.MonkeyPatch, spy_direct: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings, "prefect_enabled", True)
    monkeypatch.setattr(
        settings, "prefect_analytics_deployment", "analytics-export/lkm"
    )
    monkeypatch.setattr(settings, "prefect_api_url", "http://prefect-server:4200/api")
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect-server:4200/api")

    captured: dict[str, Any] = {}

    async def fake_run_deployment(
        name: str, parameters: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        captured.update(name=name, parameters=parameters, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("prefect.deployments.run_deployment", fake_run_deployment)

    await auth_tasks.export_analytics_clickhouse()

    assert captured["name"] == "analytics-export/lkm"
    assert captured["timeout"] == 0
    assert captured["parameters"] is not None  # traceparent 由触发侧注入
    assert spy_direct == []  # 触发成功不再直调


async def should_fall_back_to_direct_on_trigger_failure(
    monkeypatch: pytest.MonkeyPatch, spy_direct: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(settings, "prefect_enabled", True)
    monkeypatch.setattr(
        settings, "prefect_analytics_deployment", "analytics-export/lkm"
    )
    monkeypatch.setattr(settings, "prefect_api_url", "http://prefect-server:4200/api")

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("prefect server unreachable")

    monkeypatch.setattr("prefect.deployments.run_deployment", boom)

    await auth_tasks.export_analytics_clickhouse()

    assert len(spy_direct) == 1  # fail-open 回落直调
