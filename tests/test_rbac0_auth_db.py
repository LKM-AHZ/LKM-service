"""RBAC0 用户角色分配、审查与 auth 权威会话角色集合。"""

from unittest.mock import AsyncMock

from auth.models import Profile, User
from auth.service_authz import authorize_user
from auth.service_roles import list_user_roles, list_users_for_role, set_user_role
from tests.conftest import DB


async def test_multiple_roles_flow_into_authorization(auth_db: DB, monkeypatch) -> None:
    monkeypatch.setattr("auth.service_roles.events.notify_user_updated", AsyncMock())
    monkeypatch.setattr(
        "auth.service_roles.events.notify_audit_permission_change", AsyncMock()
    )
    user = User(username="rbac0_multi", hashed_password="test", account_level="normal")
    auth_db.add(user)
    await auth_db.flush()
    auth_db.add(Profile(user_id=user.id, role="member"))
    await auth_db.flush()

    assert await set_user_role(auth_db, user.id, "normal:author", assigned=True)
    assert await list_user_roles(auth_db, user.id) == (
        "normal:author",
        "normal:member",
    )
    assert user.id in await list_users_for_role(auth_db, "normal:author")

    verdict = await authorize_user(
        auth_db,
        user_id=user.id,
        expect_token_version=1,
        iat_ts=None,
        require_admin=False,
    )
    assert verdict["ok"] is True
    assert verdict["active_roles"] == ("normal:author", "normal:member")

    assert await set_user_role(auth_db, user.id, "normal:author", assigned=False)
    assert await list_user_roles(auth_db, user.id) == ("normal:member",)
    assert user.id not in await list_users_for_role(auth_db, "normal:author")
