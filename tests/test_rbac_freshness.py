"""授权变更后下一次权限检查读取当前数据库状态。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.dialects import postgresql

from app.modules.rbac.permissions import Permission
from app.modules.rbac.repository import RolePermissionRepository
from app.modules.rbac.seed import seed_rbac
from app.modules.rbac.service import role_has_permission, set_role_permission


async def test_revoked_grant_is_not_reused(monkeypatch) -> None:
    lookup = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(RolePermissionRepository, "has_permission", lookup)

    assert await role_has_permission(None, "normal:member", Permission.content_create)
    assert not await role_has_permission(
        None, "normal:member", Permission.content_create
    )
    assert lookup.await_count == 2
    lookup.assert_awaited_with("normal:member", Permission.content_create.value)


async def test_permission_lookup_excludes_disabled_rows() -> None:
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    assert not await RolePermissionRepository(db).has_permission(
        "normal:member", Permission.content_create.value
    )
    stmt = db.scalar.await_args.args[0]
    sql = str(stmt.whereclause.compile(dialect=postgresql.dialect()))
    assert "role_permissions.enabled IS true" in sql


async def test_multi_role_lookup_uses_one_enabled_query() -> None:
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    assert not await RolePermissionRepository(db).has_any_permission(
        ("normal:member", "normal:author"), Permission.projects_create.value
    )
    db.scalar.assert_awaited_once()
    stmt = db.scalar.await_args.args[0]
    sql = str(stmt.whereclause.compile(dialect=postgresql.dialect()))
    assert "role_permissions.role_name IN" in sql
    assert "role_permissions.enabled IS true" in sql


async def test_setting_permission_uses_atomic_upsert() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)))
    assert await set_role_permission(
        db, "normal:member", Permission.content_create, enabled=False
    )
    stmt = db.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (role_name, permission) DO UPDATE" in sql
    assert "IS DISTINCT FROM" in sql


async def test_seed_does_not_overwrite_explicit_revocation() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=0)),
        flush=AsyncMock(),
    )
    await seed_rbac(db)
    stmt = db.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (role_name, permission) DO NOTHING" in sql
    db.execute.assert_awaited_once()
