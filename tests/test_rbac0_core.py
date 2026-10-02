"""RBAC0 多角色分配与会话权限并集。"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.modules.rbac.permissions import DEFAULT_GRANTS, Permission
from app.modules.rbac.service import user_has_permission
from auth import user_http
from auth.admin_router import _require_role_manager
from auth.deps import _resolve_via_seam
from auth.repository import UserRoleRepository
from auth.service_roles import list_users_for_role, set_user_role
from core.contracts import CurrentUser
from core.err import BizError, CommonErr
from core.rbac_roles import KNOWN_ROLES, activated_roles


def test_declared_roles_match_default_permission_map() -> None:
    assert DEFAULT_GRANTS.keys() == KNOWN_ROLES


def test_session_activates_all_current_level_assignments() -> None:
    assert activated_roles(
        "normal", "member", ["normal:author", "admin:super_admin", "normal:author"]
    ) == ("normal:author", "normal:member")


async def test_permissions_are_union_of_active_roles(monkeypatch) -> None:
    from app.modules.rbac.repository import RolePermissionRepository

    lookup = AsyncMock(return_value=True)
    monkeypatch.setattr(RolePermissionRepository, "has_any_permission", lookup)
    cur = CurrentUser(
        id=uuid.uuid4(),
        account_level="normal",
        role="member",
        active_roles=("normal:member", "normal:author"),
    )
    assert await user_has_permission(None, cur, Permission.projects_create)
    lookup.assert_awaited_once_with(
        ("normal:author", "normal:columnist", "normal:member"),
        Permission.projects_create.value,
    )


async def test_explicit_empty_session_roles_deny(monkeypatch) -> None:
    lookup = AsyncMock(return_value=True)
    monkeypatch.setattr("app.modules.rbac.service.role_has_permission", lookup)
    cur = CurrentUser(
        id=uuid.uuid4(), account_level="normal", role="member", active_roles=()
    )
    assert not await user_has_permission(None, cur, Permission.content_create)
    lookup.assert_not_awaited()


async def test_old_current_user_uses_primary_role(monkeypatch) -> None:
    lookup = AsyncMock(return_value=True)
    monkeypatch.setattr("app.modules.rbac.service.role_has_permission", lookup)
    cur = CurrentUser(id=uuid.uuid4(), account_level="normal", role="member")
    assert await user_has_permission(None, cur, Permission.content_create)
    assert lookup.await_args.args[1] == "normal:member"


async def test_auth_verdict_carries_active_roles(monkeypatch) -> None:
    verdict = AsyncMock(
        return_value={
            "ok": True,
            "account_level": "normal",
            "role": "member",
            "active_roles": ["normal:member", "normal:author"],
        }
    )
    monkeypatch.setattr("auth.user_http.authorize_via_seam", verdict)
    cur = await _resolve_via_seam(uuid.uuid4(), 0, 1, require_admin=False)
    assert cur.active_roles == ("normal:member", "normal:author")


async def test_auth_verdict_must_honor_selected_roles(monkeypatch) -> None:
    monkeypatch.setattr(
        "auth.user_http.authorize_via_seam",
        AsyncMock(
            return_value={
                "ok": True,
                "account_level": "normal",
                "role": "member",
                "active_roles": ["normal:member"],
            }
        ),
    )
    with pytest.raises(BizError):
        await _resolve_via_seam(
            uuid.uuid4(), 0, 1, require_admin=False, selected_roles=()
        )


async def test_auth_seam_rejects_malformed_role_set(monkeypatch) -> None:
    monkeypatch.setattr(
        user_http,
        "_request",
        AsyncMock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "account_level": "normal",
                    "role": "member",
                    "active_roles": ["normal:member", 7],
                },
            )
        ),
    )
    with pytest.raises(user_http.UserHttpUnavailable):
        await user_http.authorize_via_seam(
            user_id=uuid.uuid4(),
            expect_token_version=0,
            iat_ts=None,
            require_admin=False,
        )


async def test_auth_seam_rejects_missing_success_role_set(monkeypatch) -> None:
    monkeypatch.setattr(
        user_http,
        "_request",
        AsyncMock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "account_level": "normal", "role": "member"},
            )
        ),
    )
    with pytest.raises(user_http.UserHttpUnavailable):
        await user_http.authorize_via_seam(
            user_id=uuid.uuid4(),
            expect_token_version=0,
            iat_ts=None,
        )


async def test_assign_extra_role_revokes_old_sessions(monkeypatch) -> None:
    user_id = uuid.uuid4()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[SimpleNamespace(first=lambda: ("normal", "member")), None]
        )
    )
    add = AsyncMock(return_value=True)
    updated = AsyncMock()
    audited = AsyncMock()
    monkeypatch.setattr(UserRoleRepository, "add", add)
    monkeypatch.setattr(UserRoleRepository, "list_roles", AsyncMock(return_value=[]))
    monkeypatch.setattr("auth.service_roles.events.notify_user_updated", updated)
    monkeypatch.setattr(
        "auth.service_roles.events.notify_audit_permission_change", audited
    )

    assert await set_user_role(db, user_id, "normal:author", assigned=True)
    add.assert_awaited_once_with(user_id, "normal:author")
    assert db.execute.await_count == 2
    updated.assert_awaited_once_with(user_id)
    audited.assert_awaited_once()


async def test_user_role_assignment_is_atomic() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)))
    assert await UserRoleRepository(db).add(uuid.uuid4(), "normal:author")
    stmt = db.execute.await_args.args[0]
    assert "ON CONFLICT (user_id, role_name) DO NOTHING" in str(
        stmt.compile(dialect=postgresql.dialect())
    )


async def test_role_review_includes_primary_and_assigned_users() -> None:
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=list)))
    assert await list_users_for_role(db, "normal:member") == []
    stmt = db.scalars.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "UNION" in sql
    assert "user_roles" in sql
    assert "profiles.user_id IS NULL" in sql


async def test_assignment_rejects_other_account_level() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(first=lambda: ("normal", "member"))
        )
    )
    with pytest.raises(BizError) as exc:
        await set_user_role(db, uuid.uuid4(), "admin:super_admin", assigned=True)
    assert exc.value.errcode == CommonErr.INVALID_INPUT


async def test_primary_role_cannot_be_revoked(monkeypatch) -> None:
    user_id = uuid.uuid4()
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(first=lambda: ("normal", "member"))
        )
    )
    remove = AsyncMock()
    monkeypatch.setattr(UserRoleRepository, "remove", remove)
    with pytest.raises(BizError) as exc:
        await set_user_role(db, user_id, "normal:member", assigned=False)
    assert exc.value.errcode == CommonErr.INVALID_INPUT
    remove.assert_not_awaited()


async def test_role_management_requires_mfa(monkeypatch) -> None:
    actor = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        "auth.admin_router._require_admin_from_cookie", AsyncMock(return_value=actor)
    )
    monkeypatch.setattr("auth.admin_router._current_mfa_trust", lambda _: (False, None))
    with pytest.raises(BizError) as exc:
        await _require_role_manager(None, None)
    assert exc.value.errcode == CommonErr.MFA_REQUIRED


async def test_role_management_requires_super_admin(monkeypatch) -> None:
    actor = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        "auth.admin_router._require_admin_from_cookie", AsyncMock(return_value=actor)
    )
    monkeypatch.setattr("auth.admin_router._current_mfa_trust", lambda _: (True, 1))
    monkeypatch.setattr(
        "auth.admin_router._active_cookie_roles",
        AsyncMock(return_value=("admin:org_member",)),
    )
    with pytest.raises(BizError) as exc:
        await _require_role_manager(None, None)
    assert exc.value.errcode == CommonErr.FORBIDDEN
