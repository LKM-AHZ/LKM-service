"""后台分析查询端点 /admin/analytics/{dataset} 的 HTTP 集成测试（M5 7.2.6）。

覆盖：非 admin 拒绝、admin+权限点返回分页、dataset 白名单拒未知、limit 裁剪到配置上限、
时间窗参数化、CH 未启用 → 503（不返回空列表冒充无数据）。

CH 客户端经 ``get_analytics_client`` 依赖 override 注入 fake，不依赖真实 ClickHouse。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.main import app
from app.modules.admin.analytics_router import get_analytics_client
from app.modules.rbac.permissions import Permission
from tests.fakes import FakeClickHouseClient
from tests.test_admin_reports import _grant, _mk_admin, _mk_member, _set_admin_cookie

_APP_LOG_COLUMNS = [
    "ts",
    "level",
    "logger",
    "msg",
    "request_id",
    "trace_id",
    "span_id",
    "service",
]
_APP_LOG_ROW = (
    "2026-09-14 03:00:00.000",
    "info",
    "lkm.http",
    "hello",
    "req-1",
    "trace-1",
    "span-1",
    "backend",
)


@pytest.fixture
def fake_ch() -> Iterator[FakeClickHouseClient]:
    """把 analytics 查询的 CH 依赖覆盖为内存 fake。"""
    fake = FakeClickHouseClient(count=1, rows=[_APP_LOG_ROW], columns=_APP_LOG_COLUMNS)

    async def _dep() -> FakeClickHouseClient:
        return fake

    app.dependency_overrides[get_analytics_client] = _dep
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_analytics_client, None)


async def should_reject_non_admin(
    client: AsyncClient, auth_db: AsyncSession, fake_ch: FakeClickHouseClient
) -> None:
    member = await _mk_member(auth_db, "member1")
    _set_admin_cookie(client, member)

    resp = await client.get("/api/v1/admin/analytics/app_logs")

    assert resp.status_code in (401, 403)


async def should_return_paginated_rows_for_admin(
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    fake_ch: FakeClickHouseClient,
) -> None:
    root = await _mk_admin(auth_db, "root")
    await _grant(db, Permission.admin_analytics_view)
    _set_admin_cookie(client, root)

    resp = await client.get("/api/v1/admin/analytics/app_logs", params={"limit": 50})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["level"] == "info"
    assert body["data"]["items"][0]["service"] == "backend"


async def should_reject_unknown_dataset(
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    fake_ch: FakeClickHouseClient,
) -> None:
    root = await _mk_admin(auth_db, "root")
    await _grant(db, Permission.admin_analytics_view)
    _set_admin_cookie(client, root)

    resp = await client.get("/api/v1/admin/analytics/not_a_table")

    assert resp.status_code == 422
    assert "unknown dataset" in resp.json()["message"]


async def should_clamp_limit_to_configured_max(
    monkeypatch: pytest.MonkeyPatch,
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    fake_ch: FakeClickHouseClient,
) -> None:
    monkeypatch.setattr(settings, "clickhouse_query_limit_max", 200)
    root = await _mk_admin(auth_db, "root")
    await _grant(db, Permission.admin_analytics_view)
    _set_admin_cookie(client, root)

    resp = await client.get("/api/v1/admin/analytics/app_logs", params={"limit": 1000})

    assert resp.status_code == 200
    last_sql, last_params = fake_ch.queries[-1]
    assert last_params["lim"] == 200  # 1000 被裁到配置上限
    assert last_params["off"] == 0
    assert "{lim:UInt32}" in last_sql  # 参数化占位，非字面量拼接


async def should_parameterize_time_window(
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    fake_ch: FakeClickHouseClient,
) -> None:
    root = await _mk_admin(auth_db, "root")
    await _grant(db, Permission.admin_analytics_view)
    _set_admin_cookie(client, root)

    resp = await client.get(
        "/api/v1/admin/analytics/app_logs",
        params={"since": "2026-09-01T00:00:00+00:00"},
    )

    assert resp.status_code == 200
    last_sql, last_params = fake_ch.queries[-1]
    assert "since" in last_params
    assert "{since:DateTime64(3)}" in last_sql


async def should_return_503_when_clickhouse_disabled(
    monkeypatch: pytest.MonkeyPatch,
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    # 不注入 fake_ch：走真实 get_analytics_client，未启用时抛 503
    monkeypatch.setattr(settings, "clickhouse_enabled", False)
    monkeypatch.setattr(settings, "clickhouse_url", "")
    root = await _mk_admin(auth_db, "root")
    await _grant(db, Permission.admin_analytics_view)
    _set_admin_cookie(client, root)

    resp = await client.get("/api/v1/admin/analytics/app_logs")

    assert resp.status_code == 503
