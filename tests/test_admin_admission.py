"""后台准入审核端点：权限点、2FA、列表、approve/reject 与会话撤销。"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from auth.models import AuditLog, Profile, User
from core.db.base import now_iso
from core.err import CommonErr
from tests.conftest import DB, Client


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam(auth_seam_fused: None) -> None:
    """require_admin 经 seam 裁决到本测 auth 表。"""


async def _mk_admin(db: DB, username: str = "admission_admin") -> User:
    user = User(
        username=username,
        account_level="admin",
        hashed_password="admin-placeholder-not-real",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="super_admin"))
    await db.flush()
    return user


async def _mk_pending(db: DB, username: str = "pending_user") -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="pending-placeholder-not-real",
        account_level="normal",
        admission_status="pending",
        applied_at=now_iso(),
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


async def _grant(db: DB, permission: str = "admin.users_review") -> None:
    db.add(RolePermission(role_name="admin:super_admin", permission=permission))
    await db.flush()


def _cookie(client: Client, user: User, *, mfa: bool = True) -> None:
    token = create_admin_access_token(user, mfa_verified=mfa)
    client.cookies.set(COOKIE_NAME, token, path=COOKIE_PATH)


async def test_list_requires_permission(db: DB, client: Client) -> None:
    admin = await _mk_admin(db)
    await _mk_pending(db)
    _cookie(client, admin)

    resp = await client.get("/api/v1/admin/admission/users")

    assert resp.status_code == 403


async def test_list_returns_pending(db: DB, client: Client) -> None:
    await _grant(db)
    admin = await _mk_admin(db)
    pending = await _mk_pending(db)
    _cookie(client, admin)

    resp = await client.get("/api/v1/admin/admission/users")

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert [row["user_id"] for row in rows] == [str(pending.id)]
    assert rows[0]["admission_status"] == "pending"


async def test_decision_requires_2fa(db: DB, client: Client) -> None:
    await _grant(db)
    admin = await _mk_admin(db)
    pending = await _mk_pending(db)
    _cookie(client, admin, mfa=False)

    resp = await client.post(
        f"/api/v1/admin/admission/users/{pending.id}/decision",
        json={"action": "approve"},
    )

    assert resp.status_code == 401
    assert resp.json()["code"] == CommonErr.MFA_REQUIRED


async def test_approve_activates_and_audits(db: DB, client: Client) -> None:
    await _grant(db)
    admin = await _mk_admin(db)
    pending = await _mk_pending(db)
    _cookie(client, admin)

    resp = await client.post(
        f"/api/v1/admin/admission/users/{pending.id}/decision",
        json={"action": "approve"},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["admission_status"] == "active"
    await db.refresh(pending)
    assert pending.admission_status == "active"
    assert pending.admission_reviewed_by == admin.id
    audit = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "admission_approved")
        )
    ).scalar_one()
    assert audit.user_id == pending.id
    assert f"admin={admin.id}" in (audit.detail or "")


async def test_reject_requires_reason(db: DB, client: Client) -> None:
    await _grant(db)
    admin = await _mk_admin(db)
    pending = await _mk_pending(db)
    _cookie(client, admin)

    resp = await client.post(
        f"/api/v1/admin/admission/users/{pending.id}/decision",
        json={"action": "reject", "reason": "  "},
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == CommonErr.INVALID_INPUT


async def test_reject_sets_status_and_revokes(db: DB, client: Client) -> None:
    await _grant(db)
    admin = await _mk_admin(db)
    pending = await _mk_pending(db)
    _cookie(client, admin)
    version_before = pending.token_version

    resp = await client.post(
        f"/api/v1/admin/admission/users/{pending.id}/decision",
        json={"action": "reject", "reason": "资料不完整"},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["admission_status"] == "rejected"
    await db.refresh(pending)
    assert pending.reject_reason == "资料不完整"
    assert pending.token_version == version_before + 1

    again = await client.post(
        f"/api/v1/admin/admission/users/{pending.id}/decision",
        json={"action": "approve"},
    )
    assert again.status_code == 409
