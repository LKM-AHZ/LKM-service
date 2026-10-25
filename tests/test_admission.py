"""P2 准入：apply 模式注册待审、登录门禁、approve/reject、拒绝撤销会话。

默认 ``open`` 行为不变（见既有注册/登录用例）；本文件只覆盖 apply 分支与审核动作。
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.admission import ACTIVE, PENDING, REJECTED
from auth.errors import AuthErr
from auth.models import RefreshToken, User
from auth.schemas import UserLoginPassword, UserRegLocal
from auth.security import hashpwd
from auth.service_admission import decide_admission, list_admission_users
from auth.service_auth import (
    consume_pending_normal_registration,
    create_user_with_profile,
    issue_session_tokens,
    login_password,
    register_local,
    store_pending_normal_registration,
    store_refresh_token,
)
from auth.service_authz import authorize_user
from auth.service_verify import create_email_verification
from core.config import settings
from core.err import BizError, CommonErr


@pytest.fixture
def apply_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admission_mode", "apply")


async def _apply_user(db: AsyncSession, username: str = "applicant") -> User:
    user = await create_user_with_profile(
        db,
        username=username,
        hashed_password=await hashpwd("secret123456"),
        account_level="normal",
    )
    await db.flush()
    return user


async def test_apply_mode_creates_pending_user(
    auth_db: AsyncSession, apply_mode: None
) -> None:
    user = await _apply_user(auth_db)

    assert user.admission_status == PENDING
    assert user.applied_at is not None

    with pytest.raises(BizError) as exc:
        await login_password(
            auth_db, UserLoginPassword(account="applicant", password="secret123456")
        )
    assert exc.value.errcode == AuthErr.ADMISSION_PENDING


async def test_approve_allows_login(auth_db: AsyncSession, apply_mode: None) -> None:
    user = await _apply_user(auth_db)
    reviewer = uuid.uuid4()

    row = await decide_admission(
        auth_db, user_id=user.id, action="approve", reviewer_id=reviewer
    )

    assert row["admission_status"] == ACTIVE
    assert row["reviewed_at"] is not None
    result = await login_password(
        auth_db, UserLoginPassword(account="applicant", password="secret123456")
    )
    assert result["access_token"]
    assert user.admission_reviewed_by == reviewer


async def test_reject_blocks_login_and_revokes_sessions(
    auth_db: AsyncSession, apply_mode: None
) -> None:
    user = await _apply_user(auth_db)
    raw_refresh = "admission-refresh-token-" + "x" * 32
    await store_refresh_token(auth_db, user.id, raw_refresh)
    version_before = user.token_version

    row = await decide_admission(
        auth_db,
        user_id=user.id,
        action="reject",
        reviewer_id=uuid.uuid4(),
        reason="资料不完整",
    )

    assert row["admission_status"] == REJECTED
    assert row["reject_reason"] == "资料不完整"
    await auth_db.refresh(user)
    assert user.token_version == version_before + 1
    stored = (
        (
            await auth_db.execute(
                select(RefreshToken).where(RefreshToken.token_hash.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    assert stored and all(token.revoked_at is not None for token in stored)

    with pytest.raises(BizError) as exc:
        await login_password(
            auth_db, UserLoginPassword(account="applicant", password="secret123456")
        )
    assert exc.value.errcode == AuthErr.ADMISSION_REJECTED
    assert exc.value.detail == "资料不完整"


async def test_decide_only_pending(auth_db: AsyncSession, apply_mode: None) -> None:
    user = await _apply_user(auth_db)
    reviewer = uuid.uuid4()
    await decide_admission(
        auth_db, user_id=user.id, action="approve", reviewer_id=reviewer
    )

    with pytest.raises(BizError) as exc:
        await decide_admission(
            auth_db, user_id=user.id, action="reject", reviewer_id=reviewer, reason="x"
        )
    assert exc.value.errcode == CommonErr.CONFLICT


async def test_list_admission_users_filters_status(
    auth_db: AsyncSession, apply_mode: None
) -> None:
    pending = await _apply_user(auth_db, "p1")
    approved = await _apply_user(auth_db, "p2")
    await decide_admission(
        auth_db, user_id=approved.id, action="approve", reviewer_id=uuid.uuid4()
    )

    rows = await list_admission_users(auth_db, PENDING)

    assert [row["user_id"] for row in rows] == [str(pending.id)]


async def test_pending_user_cannot_get_tokens(
    auth_db: AsyncSession, apply_mode: None
) -> None:
    user = await _apply_user(auth_db)

    with pytest.raises(BizError) as exc:
        await issue_session_tokens(auth_db, user)
    assert exc.value.errcode == AuthErr.ADMISSION_PENDING

    verdict = await authorize_user(
        auth_db,
        user_id=user.id,
        expect_token_version=user.token_version,
        iat_ts=None,
        require_admin=False,
    )
    assert verdict["ok"] is False
    assert verdict["cause"] == "locked"


async def test_http_registration_returns_pending(
    auth_db: AsyncSession,
    auth_app_client: AsyncClient,
    apply_mode: None,
) -> None:
    """apply 模式注册端点：200 + 无 token + admission_status=pending。"""
    resp = await auth_app_client.post(
        "/api/v1/auth/reg/local",
        json={"username": "http_pending", "password": "secret123456"},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["access_token"] is None
    assert data["admission_status"] == "pending"


async def test_local_registration_stores_apply_reason(
    auth_db: AsyncSession, apply_mode: None
) -> None:
    resp = await register_local(
        auth_db,
        UserRegLocal(
            username="reason_local",
            password="secret123456",
            apply_reason="想加入社区一起整理资料",
        ),
    )

    assert resp["admission_status"] == PENDING
    user = (
        await auth_db.execute(select(User).where(User.username == "reason_local"))
    ).scalar_one()
    assert user.admission_status == PENDING
    assert user.apply_reason == "想加入社区一起整理资料"


async def test_pending_normal_registration_carries_reason(
    auth_db: AsyncSession, apply_mode: None
) -> None:
    code, _ = await create_email_verification(
        auth_db, "reason_norm@example.com", "register"
    )
    txn = await store_pending_normal_registration(
        auth_db,
        "reason_norm",
        "secret123456",
        "reason_norm@example.com",
        None,
        apply_reason="数学爱好者",
    )

    result = await consume_pending_normal_registration(auth_db, txn, email_code=code)

    assert result["admission_status"] == PENDING
    user = (
        await auth_db.execute(select(User).where(User.username == "reason_norm"))
    ).scalar_one()
    assert user.apply_reason == "数学爱好者"
