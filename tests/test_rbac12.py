"""RBAC1 hierarchy and RBAC2 SSD/DSD policy behavior."""

import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

import core.rbac_roles as rbac_roles
from app.modules.content.boards.router import owner_update_board
from app.modules.content.boards.schemas import BoardUpdate
from app.modules.rbac.permissions import Permission
from app.modules.rbac.repository import RolePermissionRepository
from app.modules.rbac.service import user_has_permission
from auth.deps import CurrentUser as AuthCurrentUser
from auth.repository import UserRoleRepository
from auth.router import _ActivateRolesRequest, activate_roles
from auth.service_auth import refresh_access_token
from auth.service_authz import CAUSE_SESSION_REVOKED, authorize_user
from auth.service_roles import list_users_for_role, set_user_role
from core.contracts import CurrentUser
from core.err import BizError, CommonErr
from core.rbac_roles import (
    SeparationConstraint,
    _validate_hierarchy,
    activated_roles,
    role_closure,
    satisfies_constraints,
)


def test_hierarchy_is_transitive() -> None:
    assert role_closure(("normal:author",)) == (
        "normal:author",
        "normal:columnist",
        "normal:member",
    )
    assert "normal:member" not in role_closure(("admin:super_admin",))
    assert activated_roles(
        "normal", "member", ["normal:author"], ["normal:columnist"]
    ) == ("normal:columnist",)


def test_hierarchy_rejects_cycle(monkeypatch) -> None:
    monkeypatch.setitem(
        rbac_roles.ROLE_INHERITANCE,
        "normal:member",
        frozenset({"normal:author"}),
    )
    with pytest.raises(ValueError, match="cycle"):
        _validate_hierarchy()


async def test_junior_permission_flows_to_active_senior(monkeypatch) -> None:
    lookup = AsyncMock(return_value=True)
    monkeypatch.setattr(RolePermissionRepository, "has_any_permission", lookup)
    cur = CurrentUser(
        id=uuid.uuid4(),
        account_level="normal",
        role="author",
        active_roles=("normal:author",),
    )
    assert await user_has_permission(None, cur, Permission.columns_publish)
    lookup.assert_awaited_once_with(
        ("normal:author", "normal:columnist", "normal:member"),
        Permission.columns_publish.value,
    )


async def test_ssd_rejects_conflicting_assignment(monkeypatch) -> None:
    constraint = SeparationConstraint(
        frozenset({"admin:content_reviewer", "admin:content_publisher"}), 1
    )
    monkeypatch.setattr("auth.service_roles.SSD_CONSTRAINTS", (constraint,))
    monkeypatch.setattr(
        UserRoleRepository,
        "list_roles",
        AsyncMock(return_value=["admin:content_reviewer"]),
    )
    add = AsyncMock()
    monkeypatch.setattr(UserRoleRepository, "add", add)
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(first=lambda: ("admin", "org_member"))
        )
    )
    with pytest.raises(BizError) as exc:
        await set_user_role(db, uuid.uuid4(), "admin:content_publisher", assigned=True)
    assert exc.value.errcode == CommonErr.INVALID_INPUT
    add.assert_not_awaited()


async def test_ssd_rejects_assignment_conflicting_with_inherited_role(
    monkeypatch,
) -> None:
    constraint = SeparationConstraint(
        frozenset({"normal:author", "normal:columnist"}), 1
    )
    monkeypatch.setattr("auth.service_roles.SSD_CONSTRAINTS", (constraint,))
    monkeypatch.setattr(UserRoleRepository, "list_roles", AsyncMock(return_value=[]))
    add = AsyncMock()
    monkeypatch.setattr(UserRoleRepository, "add", add)
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(first=lambda: ("normal", "member"))
        )
    )
    with pytest.raises(BizError) as exc:
        await set_user_role(db, uuid.uuid4(), "normal:author", assigned=True)
    assert exc.value.errcode == CommonErr.INVALID_INPUT
    add.assert_not_awaited()


def test_dsd_allows_assignment_but_restricts_each_session(monkeypatch) -> None:
    constraint = SeparationConstraint(
        frozenset({"admin:content_reviewer", "admin:content_publisher"}), 1
    )
    monkeypatch.setattr("core.rbac_roles.DSD_CONSTRAINTS", (constraint,))
    assigned = ["admin:content_reviewer", "admin:content_publisher"]
    assert satisfies_constraints(assigned, ())
    assert activated_roles(
        "admin", "org_member", assigned, ["admin:content_reviewer"]
    ) == ("admin:content_reviewer",)
    with pytest.raises(ValueError, match="DSD"):
        activated_roles("admin", "org_member", assigned, assigned)
    assert (
        len(set(activated_roles("admin", "org_member", assigned)) & constraint.roles)
        == 1
    )


def test_session_rejects_unassigned_role() -> None:
    with pytest.raises(ValueError, match="not authorized"):
        activated_roles("admin", "org_member", [], ["admin:content_reviewer"])


def test_ssd_counts_inherited_junior() -> None:
    constraint = SeparationConstraint(
        frozenset({"normal:author", "normal:columnist"}), 1
    )
    assert not satisfies_constraints(("normal:author",), (constraint,))


def test_dsd_falls_back_to_inherited_junior(monkeypatch) -> None:
    constraint = SeparationConstraint(
        frozenset({"normal:author", "normal:columnist"}), 1
    )
    monkeypatch.setattr("core.rbac_roles.DSD_CONSTRAINTS", (constraint,))
    # The senior assignment remains valid under SSD, but its own hierarchy
    # closure conflicts with DSD. The session can still activate its junior.
    assert activated_roles("normal", "author", []) == ("normal:columnist",)
    with pytest.raises(ValueError, match="DSD"):
        activated_roles("normal", "author", [], ["normal:author"])
    assert activated_roles("normal", "author", [], ["normal:columnist"]) == (
        "normal:columnist",
    )


@pytest.mark.parametrize(
    ("primary_role", "delegated"),
    [("org_member", True), ("super_admin", False)],
)
async def test_board_delegation_uses_active_permission(
    monkeypatch, primary_role: str, delegated: bool
) -> None:
    owner_check = AsyncMock(return_value=delegated)
    update = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr("app.modules.content.boards.router.check_owner", owner_check)
    monkeypatch.setattr("app.modules.content.boards.router.update_board_ex", update)
    cur = CurrentUser(
        id=uuid.uuid4(),
        account_level="admin",
        role=primary_role,
        active_roles=("admin:content_reviewer",) if delegated else (),
    )
    await owner_update_board.__wrapped__(
        uuid.uuid4(), BoardUpdate(title="updated"), cur, None
    )
    assert update.await_args.kwargs["is_admin"] is delegated


async def test_role_review_includes_senior_assignments() -> None:
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    )
    assert await list_users_for_role(db, "normal:columnist") == []
    stmt = db.scalars.await_args.args[0]
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "normal:author" in sql
    assert "normal:columnist" in sql


async def test_authz_rejects_unassigned_session_claim(monkeypatch) -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        token_version=0,
        is_locked=False,
        locked_until=None,
        updated_at=None,
        account_level="admin",
        profile=SimpleNamespace(role="org_member"),
    )
    monkeypatch.setattr(
        "auth.service_authz.UserRepository.get_with_profile",
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr(UserRoleRepository, "list_roles", AsyncMock(return_value=[]))
    verdict = await authorize_user(
        None,
        user_id=user_id,
        expect_token_version=0,
        iat_ts=None,
        require_admin=True,
        selected_roles=("admin:content_reviewer",),
    )
    assert verdict["ok"] is False
    assert verdict["cause"] == CAUSE_SESSION_REVOKED


async def test_authz_returns_explicit_empty_session(monkeypatch) -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        token_version=0,
        is_locked=False,
        locked_until=None,
        updated_at=None,
        account_level="admin",
        profile=SimpleNamespace(role="org_member"),
    )
    monkeypatch.setattr(
        "auth.service_authz.UserRepository.get_with_profile",
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr(UserRoleRepository, "list_roles", AsyncMock(return_value=[]))
    verdict = await authorize_user(
        None,
        user_id=user_id,
        expect_token_version=0,
        iat_ts=None,
        require_admin=True,
        selected_roles=(),
    )
    assert verdict["ok"] is True
    assert verdict["active_roles"] == ()


async def test_role_activation_binds_refresh_session(monkeypatch) -> None:
    stored = SimpleNamespace(active_roles=None)
    db = SimpleNamespace(scalar=AsyncMock(return_value=stored))
    monkeypatch.setattr(
        UserRoleRepository,
        "list_roles",
        AsyncMock(return_value=["admin:content_reviewer"]),
    )
    monkeypatch.setattr(
        "auth.router.decode_access_token",
        lambda _: {"token_version": 3, "mfa": False},
    )
    mint = Mock(return_value="scoped-token")
    monkeypatch.setattr("auth.router.create_access_token", mint)
    cur = AuthCurrentUser(id=uuid.uuid4(), account_level="admin", role="org_member")
    result = await activate_roles.__wrapped__(
        _ActivateRolesRequest(
            roles=["admin:content_reviewer"], refresh_token="refresh-secret"
        ),
        token="signed-access",
        cur=cur,
        db=db,
    )
    assert result == {"access_token": "scoped-token"}
    assert stored.active_roles == ["admin:content_reviewer"]
    assert mint.call_args.kwargs["active_roles"] == ("admin:content_reviewer",)


async def test_refresh_preserves_selected_roles(monkeypatch) -> None:
    user_id = uuid.uuid4()
    stored = SimpleNamespace(
        user_id=user_id,
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1),
        mfa_verified=False,
        mfa_at=None,
        active_roles=["admin:content_reviewer"],
    )
    monkeypatch.setattr("auth.service_auth.consume_once", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "auth.service_auth.get_or_raise", AsyncMock(return_value=stored)
    )
    monkeypatch.setattr(
        "auth.service_auth.UserRepository.get_with_profile_or_raise",
        AsyncMock(return_value=SimpleNamespace(id=user_id)),
    )
    issue = AsyncMock(return_value=("new-access", "new-refresh"))
    monkeypatch.setattr("auth.service_auth.issue_session_tokens", issue)
    result = await refresh_access_token(None, "old-refresh")
    assert result == {"access_token": "new-access", "refresh_token": "new-refresh"}
    assert issue.await_args.kwargs["active_roles"] == ("admin:content_reviewer",)
