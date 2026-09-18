"""Tests for Magic Link login (Task 13)."""

import datetime as dt
import hashlib
import secrets
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import TOTP, MagicLink, User
from app.modules.auth.providers.console import ConsoleEmailProvider


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """auth 域单测跑在 auth 独立库 schema（S5 拆后 users 不在 biz）。"""
    return auth_db


def _service():
    from app.modules.auth import service_auth

    return service_auth


async def _create_user(
    db: AsyncSession,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "secret123456",
    account_level: str = "normal",
) -> User:
    """Create a user with the given parameters and return it."""
    from app.modules.auth.models import Profile
    from app.modules.auth.security import hashpwd

    user = User(
        username=username,
        email=email,
        hashed_password=await hashpwd(password),
        account_level=account_level,
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


async def _make_magic_link(
    db: AsyncSession,
    email: str,
    purpose: str = "login",
    used: bool = False,
    expired: bool = False,
) -> tuple[str, MagicLink]:
    """Create a MagicLink record and return (raw_token, record)."""
    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    if expired:
        expires = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    else:
        expires = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15)

    record = MagicLink(
        email=email,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expires,
        used=used,
    )
    db.add(record)
    await db.flush()
    return raw_token, record


class TestRequestMagicLink:
    async def should_persist_magic_link_record(self, db: AsyncSession):
        await _create_user(db, username="testuser", email="test@example.com")
        svc = _service()
        await svc.request_magic_link(
            db,
            "test@example.com",
            ConsoleEmailProvider(),
            purpose="login",
        )

        records = (
            (
                await db.execute(
                    select(MagicLink).where(MagicLink.email == "test@example.com")
                )
            )
            .scalars()
            .all()
        )
        assert len(records) == 1
        record = records[0]
        assert record.purpose == "login"
        assert record.used is False
        assert len(record.token_hash) == 64  # SHA-256 hex

    async def should_store_different_tokens_for_repeated_requests(
        self, db: AsyncSession
    ):
        await _create_user(db, username="user_a", email="a@b.com")
        svc = _service()

        await svc.request_magic_link(
            db, "a@b.com", ConsoleEmailProvider(), purpose="login"
        )
        await svc.request_magic_link(
            db, "a@b.com", ConsoleEmailProvider(), purpose="login"
        )

        records = (
            (await db.execute(select(MagicLink).where(MagicLink.email == "a@b.com")))
            .scalars()
            .all()
        )
        assert len(records) == 2
        assert records[0].token_hash != records[1].token_hash


class TestVerifyMagicLink:
    async def should_return_auth_tokens_on_valid_link(self, db: AsyncSession):
        user = await _create_user(db, email="alice@example.com")
        raw_token, _ = await _make_magic_link(db, "alice@example.com")

        svc = _service()
        result = await svc.verify_magic_link(db, raw_token, purpose="login")

        assert result["access_token"] is not None
        assert result["refresh_token"] is not None
        assert result["user_id"] == user.id
        assert result["account_level"] == "normal"
        assert result["requires_2fa"] is False

    async def should_mark_link_as_used_after_success(self, db: AsyncSession):
        await _create_user(db, email="alice@example.com")
        raw_token, record = await _make_magic_link(db, "alice@example.com")

        svc = _service()
        await svc.verify_magic_link(db, raw_token, purpose="login")

        record_id = record.id  # snapshot before expiring the session
        db.expire_all()
        updated = cast(
            MagicLink,
            (await db.execute(select(MagicLink).where(MagicLink.id == record_id)))
            .scalars()
            .first(),
        )
        assert updated.used is True

    async def should_reject_expired_magic_link(self, db: AsyncSession):
        await _create_user(db, email="alice@example.com")
        raw_token, _ = await _make_magic_link(db, "alice@example.com", expired=True)

        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.verify_magic_link(db, raw_token, purpose="login")
        assert exc.value.errcode == AuthErr.TOKEN_EXPIRED

    async def should_reject_already_used_magic_link(self, db: AsyncSession):
        await _create_user(db, email="alice@example.com")
        raw_token, _ = await _make_magic_link(db, "alice@example.com", used=True)

        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.verify_magic_link(db, raw_token, purpose="login")
        assert exc.value.errcode == AuthErr.TOKEN_INVALID

    async def should_reject_local_user(self, db: AsyncSession):
        await _create_user(db, email="bob@local.com", account_level="local")
        raw_token, _ = await _make_magic_link(db, "bob@local.com")

        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.verify_magic_link(db, raw_token, purpose="login")
        assert exc.value.errcode == AuthErr.ACCOUNT_LEVEL_INSUFFICIENT

    async def should_reject_purpose_mismatch(self, db: AsyncSession):
        await _create_user(db, email="alice@example.com")
        raw_token, _ = await _make_magic_link(db, "alice@example.com", purpose="login")

        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.verify_magic_link(db, raw_token, purpose="reset")
        assert exc.value.errcode == AuthErr.TOKEN_INVALID

    async def should_reject_unknown_token(self, db: AsyncSession):
        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.verify_magic_link(db, "nonexistent-token", purpose="login")
        assert exc.value.errcode == AuthErr.TOKEN_INVALID

    async def should_reject_missing_user(self, db: AsyncSession):
        # No user created for this email
        raw_token, _ = await _make_magic_link(db, "no-user@example.com")

        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.verify_magic_link(db, raw_token, purpose="login")
        assert exc.value.errcode == AuthErr.USER_NOT_FOUND

    async def should_reject_admin_without_totp(self, db: AsyncSession):
        await _create_user(db, email="admin@example.com", account_level="admin")
        raw_token, _ = await _make_magic_link(db, "admin@example.com")

        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.verify_magic_link(db, raw_token, purpose="login")
        assert exc.value.errcode == AuthErr.TOTP_SETUP_REQUIRED

    async def should_login_with_totp_without_forced_2fa(self, db: AsyncSession):
        """登录不再强制 2FA：已启用 TOTP 的普通用户验证魔法链接直接得完整会话（危险操作时才 step-up）。"""
        user = await _create_user(
            db, email="secure@example.com", account_level="normal"
        )
        # enable TOTP
        totp = TOTP(user_id=user.id, secret="MZXW6YTBOJQXI33F", enabled=True)
        db.add(totp)
        await db.flush()

        raw_token, _ = await _make_magic_link(db, "secure@example.com")

        svc = _service()
        result = await svc.verify_magic_link(db, raw_token, purpose="login")
        assert result["requires_2fa"] is False
        assert result["temp_token"] is None
        assert result["access_token"] is not None
        assert result["refresh_token"] is not None
