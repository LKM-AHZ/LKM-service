"""``/admin/points-report`` 的门禁与降级契约。

端点读 continuous aggregate ``points_daily``。测试库克隆自**不带** timescaledb 扩展的模板库，
故``points_daily`` 不存在 → 端点必须返 503 而**不是**空列表（空列表会把「环境没有连续聚合」
误报成「这段时间没有任何积分行为」）。真路径（cagg 存在时的聚合结果）由
``tests/test_points_continuous_aggregate.py`` 覆盖。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.rbac.permissions import Permission
from auth.models import User
from tests.conftest import auth_user_uid  # type: ignore[attr-defined]


async def _mk_user(
    auth_db: AsyncSession,
    username: str,
    *,
    account_level: str = "normal",
    role: str = "member",
) -> User:
    """在 auth realm 造 User(+Profile)；返回 ORM（供 mint admin cookie）。"""
    au = await auth_user_uid(
        auth_db,
        username=username,
        account_level=account_level,
        role=role,
        email=None,
        nickname=username,
        with_token=False,
    )
    return (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()


def _set_admin_cookie(client: AsyncClient, user: User) -> None:
    client.cookies.set(COOKIE_NAME, create_admin_access_token(user), path=COOKIE_PATH)


async def _grant_super_admin(db: AsyncSession, *perms: Permission) -> None:
    for p in perms:
        exists = await db.scalar(
            select(RolePermission.id).where(
                RolePermission.role_name == "admin:super_admin",
                RolePermission.permission == p.value,
            )
        )
        if exists is None:
            db.add(RolePermission(role_name="admin:super_admin", permission=p.value))
    await db.flush()


async def test_points_report_rejects_non_admin(
    client: AsyncClient, auth_db: AsyncSession
) -> None:
    member = await _mk_user(auth_db, "member-points")
    _set_admin_cookie(client, member)
    resp = await client.get("/api/v1/admin/points-report")
    assert resp.status_code in (401, 403)


async def test_points_report_rejects_admin_without_permission(
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    """admin 会话但缺 ``admin.analytics_view`` 权限点 → 403（权限点才是门）。"""
    root = await _mk_user(
        auth_db, "root-points-noperm", account_level="admin", role="super_admin"
    )
    _set_admin_cookie(client, root)
    resp = await client.get("/api/v1/admin/points-report")
    assert resp.status_code == 403


async def test_points_report_503_without_continuous_aggregate(
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    root = await _mk_user(
        auth_db, "root-points", account_level="admin", role="super_admin"
    )
    await _grant_super_admin(db, Permission.admin_analytics_view)
    _set_admin_cookie(client, root)

    resp = await client.get("/api/v1/admin/points-report")
    assert resp.status_code == 503
    assert "continuous aggregate" in resp.text


async def test_points_report_200_with_continuous_aggregate(
    db: AsyncSession,
    client: AsyncClient,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    """真路径：cagg 装配并刷新后，端点返回 200 与当天聚合值。

    无 ``timescaledb`` 的库（CI 的 alpine）跳过——降级分支已由上一个用例覆盖。
    """
    import datetime
    import uuid

    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.init_db import (
        _ensure_continuous_aggregates,
        _ensure_hypertables,
        _ensure_timescaledb,
    )
    from app.modules.points.models import PointsLedger

    conn = await db.connection()
    if not await _ensure_timescaledb(conn):
        pytest.skip("本机 PG 无 timescaledb")

    root = await _mk_user(
        auth_db, "root-points-200", account_level="admin", role="super_admin"
    )
    await _grant_super_admin(db, Permission.admin_analytics_view)
    _set_admin_cookie(client, root)

    # 先 hypertable 再 cagg（顺序不可颠倒），提交后才被另开的连接看见
    await _ensure_hypertables(conn)
    await db.commit()
    engine = create_async_engine(
        db.get_bind().url.render_as_string(hide_password=False), poolclass=NullPool
    )
    try:
        await _ensure_continuous_aggregates(engine)
        db.add(
            PointsLedger(
                user_id=uuid.uuid4(),
                delta=7,
                balance_after=7,
                reason="like",
                ref_type="like",
                ref_id="http-e2e-1",
            )
        )
        await db.commit()
        async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as raw:
            await raw.execute(
                text("CALL refresh_continuous_aggregate('points_daily', NULL, NULL)")
            )
    finally:
        await engine.dispose()

    resp = await client.get("/api/v1/admin/points-report?days=1")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total_delta"] == 7
    assert data["total_entries"] == 1
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    assert data["series"] == [
        {"day": today, "reason": "like", "delta_sum": 7, "entry_count": 1}
    ]
