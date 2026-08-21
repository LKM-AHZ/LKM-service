"""Tests for password recovery (service_recovery)."""

import datetime as dt
import hashlib
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.db.models import Profile, User
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import MagicLink, RefreshToken

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _mk_local(
    db: AsyncSession, username: str = "alice", password: str = "secret123456"
) -> User:
    from app.modules.auth.security import hashpwd

    user = User(
        username=username,
        hashed_password=await hashpwd(password),
        account_level="local",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


async def _mk_normal(
    db: AsyncSession,
    username: str = "bob",
    password: str = "secret123456",
    email: str = "bob@example.com",
    phone: str = "13800001111",
) -> User:
    from app.modules.auth.security import hashpwd

    user = User(
        username=username,
        email=email,
        phone=phone,
        hashed_password=await hashpwd(password),
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


async def _mk_admin(
    db: AsyncSession,
    username: str = "admin",
    password: str = "admin123",
    email: str = "admin@example.com",
    phone: str = "13800002222",
) -> User:
    from app.modules.auth.security import hashpwd

    user = User(
        username=username,
        email=email,
        phone=phone,
        hashed_password=await hashpwd(password),
        account_level="admin",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="admin"))
    await db.flush()
    return user


def _svc():
    from app.modules.auth import service_recovery

    return service_recovery


async def should_default_new_recovery_state_and_counters(db: AsyncSession):
    import datetime as dt

    from app.modules.auth.models import RecoveryTransaction

    user = await _mk_admin(db)
    txn = RecoveryTransaction(
        txn_id="txn-model",
        user_id=user.id,
        contact=user.email,
        expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
    )
    db.add(txn)
    await db.flush()
    assert user.token_version == 0
    assert txn.state == "contact_pending"
    assert (
        txn.failed_contact_attempts,
        txn.failed_second_factor_attempts,
        txn.failed_setup_attempts,
    ) == (0, 0, 0)
    assert txn.recovery_jti_hash is None
    assert txn.completed_at is None


async def _create_phone_code(
    db: AsyncSession, phone: str, purpose: str = "reset"
) -> tuple[str, int]:
    from app.modules.auth.service_verify import create_phone_verification

    return await create_phone_verification(db, phone, purpose)


async def _create_email_code(
    db: AsyncSession, email: str, purpose: str = "reset"
) -> tuple[str, int]:
    from app.modules.auth.service_verify import create_email_verification

    return await create_email_verification(db, email, purpose)


async def _create_magic_link(
    db: AsyncSession, email: str, purpose: str = "reset"
) -> str:
    import datetime as dt
    import secrets

    from app.db.models import now_iso as _now

    raw = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires = _now() + dt.timedelta(minutes=15)

    link = MagicLink(
        email=email, token_hash=token_hash, purpose=purpose, expires_at=expires
    )
    db.add(link)
    await db.flush()
    return raw


# ===================================================================
# check_recovery_methods
# ===================================================================


class TestCheckRecoveryMethods:
    # Anti-enumeration (R3-013): check_recovery_methods always returns
    # recoverable=False regardless of whether the account exists, is local,
    # normal, or admin.  The real eligibility is determined internally by
    # the email/phone send flows.

    async def should_return_uniform_false_for_local_user(self, db: AsyncSession):
        await _mk_local(db, username="alice")
        result = await _svc().check_recovery_methods(db, "alice")
        assert result["recoverable"] is False

    async def should_return_uniform_false_for_normal_user(self, db: AsyncSession):
        await _mk_normal(
            db, username="bob", email="bob@example.com", phone="13800001111"
        )
        result = await _svc().check_recovery_methods(db, "bob")
        assert result["recoverable"] is False

    async def should_return_uniform_false_for_nonexistent(self, db: AsyncSession):
        result = await _svc().check_recovery_methods(db, "nobody")
        assert result["recoverable"] is False

    async def should_return_uniform_false_when_lookup_by_email(self, db: AsyncSession):
        await _mk_normal(db, username="bob", email="bob@example.com")
        result = await _svc().check_recovery_methods(db, "bob@example.com")
        assert result["recoverable"] is False

    async def should_return_uniform_false_when_lookup_by_phone(self, db: AsyncSession):
        await _mk_normal(db, username="bob", phone="13800001111")
        result = await _svc().check_recovery_methods(db, "13800001111")
        assert result["recoverable"] is False

    async def should_return_uniform_false_for_admin(self, db: AsyncSession):
        await _mk_admin(db, username="admin", email="admin@example.com")
        result = await _svc().check_recovery_methods(db, "admin")
        assert result["recoverable"] is False

    async def should_show_recoverable_for_normal_with_totp_enabled(
        self, db: AsyncSession
    ):
        from app.modules.auth.models import TOTP

        user = await _mk_normal(db, username="secure", email="secure@example.com")
        totp = TOTP(user_id=user.id, secret="MZXW6YTBOJQXI33F", enabled=True)
        db.add(totp)
        await db.flush()

        result = await _svc().check_recovery_methods(db, "secure")
        assert result["recoverable"] is False
        # No MFA/method leakage (R2-018)

    async def should_show_recoverable_for_normal_without_totp(self, db: AsyncSession):
        await _mk_normal(db, username="bob", email="bob@example.com")
        result = await _svc().check_recovery_methods(db, "bob")
        assert result["recoverable"] is False


# ===================================================================
# recover_by_contact（手机号通道）
# ===================================================================


class TestRecoverByContactPhone:
    async def should_reset_password_with_valid_code(self, db: AsyncSession):
        user = await _mk_normal(
            db, username="bob", password="old123", phone="13800001111"
        )
        orig_hash = user.hashed_password

        code, _ = await _create_phone_code(db, "13800001111", "reset")
        result = await _svc().recover_by_contact(db, "13800001111", code, "newpwd456")
        assert result["message"] == "Password reset successful"

        # Password was changed
        await db.refresh(user)
        assert user.hashed_password != orig_hash

        # New password works
        from app.modules.auth.security import verifypwd

        assert await verifypwd("newpwd456", user.hashed_password)

    async def should_reject_wrong_code(self, db: AsyncSession):
        await _mk_normal(db, username="bob", password="old123", phone="13800001111")
        await _create_phone_code(db, "13800001111", "reset")

        with pytest.raises(BizError) as exc:
            await _svc().recover_by_contact(db, "13800001111", "000000", "newpwd456")
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_reject_local_user(self, db: AsyncSession):
        await _mk_local(db, username="alice", password="old123")
        # give them a phone manually
        user = cast(
            User,
            (await db.execute(select(User).where(User.username == "alice")))
            .scalars()
            .first(),
        )
        user.phone = "13800003333"
        await db.flush()

        code, _ = await _create_phone_code(db, "13800003333", "reset")

        with pytest.raises(BizError) as exc:
            await _svc().recover_by_contact(db, "13800003333", code, "newpwd456")
        assert exc.value.errcode == AuthErr.RECOVERY_NOT_SUPPORTED

    async def should_reset_failed_login_attempts_and_lock(self, db: AsyncSession):
        user = await _mk_normal(
            db, username="bob", password="old123", phone="13800001111"
        )
        user.failed_login_attempts = 4
        user.is_locked = True
        user.locked_until = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
        await db.flush()

        code, _ = await _create_phone_code(db, "13800001111", "reset")
        await _svc().recover_by_contact(db, "13800001111", code, "newpwd456")

        await db.refresh(user)
        assert user.failed_login_attempts == 0
        assert user.is_locked is False
        assert user.locked_until is None

    async def should_revoke_all_refresh_tokens(self, db: AsyncSession):
        user = await _mk_normal(
            db, username="bob", password="old123", phone="13800001111"
        )
        # Add a refresh token
        import datetime as dt

        tok = RefreshToken(
            user_id=user.id,
            token_hash="abc123",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
        )
        db.add(tok)
        await db.flush()
        assert tok.revoked_at is None

        code, _ = await _create_phone_code(db, "13800001111", "reset")
        await _svc().recover_by_contact(db, "13800001111", code, "newpwd456")

        await db.refresh(tok)
        assert tok.revoked_at is not None


# ===================================================================
# recover_by_contact（邮箱通道）
# ===================================================================


class TestRecoverByContactEmail:
    async def should_reset_password_with_valid_code(self, db: AsyncSession):
        user = await _mk_normal(
            db, username="bob", password="old123", email="bob@example.com"
        )
        orig_hash = user.hashed_password

        code, _ = await _create_email_code(db, "bob@example.com", "reset")
        result = await _svc().recover_by_contact(
            db, "bob@example.com", code, "newpwd456"
        )
        assert result["message"] == "Password reset successful"

        await db.refresh(user)
        assert user.hashed_password != orig_hash

        from app.modules.auth.security import verifypwd

        assert await verifypwd("newpwd456", user.hashed_password)

    async def should_reject_wrong_code(self, db: AsyncSession):
        await _mk_normal(db, username="bob", password="old123", email="bob@example.com")
        await _create_email_code(db, "bob@example.com", "reset")

        with pytest.raises(BizError) as exc:
            await _svc().recover_by_contact(
                db, "bob@example.com", "000000", "newpwd456"
            )
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID

    async def should_reject_local_user(self, db: AsyncSession):
        await _mk_local(db, username="alice", password="old123")
        user = cast(
            User,
            (await db.execute(select(User).where(User.username == "alice")))
            .scalars()
            .first(),
        )
        user.email = "alice@example.com"
        await db.flush()

        code, _ = await _create_email_code(db, "alice@example.com", "reset")

        with pytest.raises(BizError) as exc:
            await _svc().recover_by_contact(
                db, "alice@example.com", code, "newpwd456"
            )
        assert exc.value.errcode == AuthErr.RECOVERY_NOT_SUPPORTED

    async def should_reset_failed_login_attempts_and_lock(self, db: AsyncSession):
        user = await _mk_normal(
            db, username="bob", password="old123", email="bob@example.com"
        )
        user.failed_login_attempts = 3
        user.is_locked = True
        user.locked_until = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
        await db.flush()

        code, _ = await _create_email_code(db, "bob@example.com", "reset")
        await _svc().recover_by_contact(db, "bob@example.com", code, "newpwd456")

        await db.refresh(user)
        assert user.failed_login_attempts == 0
        assert user.is_locked is False
        assert user.locked_until is None

    async def should_revoke_all_refresh_tokens(self, db: AsyncSession):
        import datetime as dt

        user = await _mk_normal(
            db, username="bob", password="old123", email="bob@example.com"
        )
        tok = RefreshToken(
            user_id=user.id,
            token_hash="def456",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
        )
        db.add(tok)
        await db.flush()
        assert tok.revoked_at is None

        code, _ = await _create_email_code(db, "bob@example.com", "reset")
        await _svc().recover_by_contact(db, "bob@example.com", code, "newpwd456")

        await db.refresh(tok)
        assert tok.revoked_at is not None


# ===================================================================
# recover_by_magic_link
# ===================================================================


class TestRecoverByMagicLink:
    async def should_reset_password_with_valid_token(self, db: AsyncSession):
        user = await _mk_normal(
            db, username="bob", password="old123", email="bob@example.com"
        )
        orig_hash = user.hashed_password

        token = await _create_magic_link(db, "bob@example.com", "reset")
        result = await _svc().recover_by_magic_link(db, token, "newpwd456")
        assert result["message"] == "Password reset successful"

        await db.refresh(user)
        assert user.hashed_password != orig_hash

        from app.modules.auth.security import verifypwd

        assert await verifypwd("newpwd456", user.hashed_password)

    async def should_reject_wrong_purpose(self, db: AsyncSession):
        await _mk_normal(db, username="bob", password="old123", email="bob@example.com")
        token = await _create_magic_link(db, "bob@example.com", "login")

        with pytest.raises(BizError) as exc:
            await _svc().recover_by_magic_link(db, token, "newpwd456")
        assert exc.value.errcode == AuthErr.TOKEN_INVALID

    async def should_reject_used_token(self, db: AsyncSession):
        await _mk_normal(db, username="bob", password="old123", email="bob@example.com")
        token = await _create_magic_link(db, "bob@example.com", "reset")

        # First use succeeds
        await _svc().recover_by_magic_link(db, token, "newpwd456")

        # Second use (replay) should fail
        with pytest.raises(BizError) as exc:
            await _svc().recover_by_magic_link(db, token, "newpwd789")
        assert exc.value.errcode == AuthErr.TOKEN_INVALID

    async def should_reject_local_user(self, db: AsyncSession):
        await _mk_local(db, username="alice", password="old123")
        user = cast(
            User,
            (await db.execute(select(User).where(User.username == "alice")))
            .scalars()
            .first(),
        )
        user.email = "alice@example.com"
        await db.flush()

        token = await _create_magic_link(db, "alice@example.com", "reset")

        with pytest.raises(BizError) as exc:
            await _svc().recover_by_magic_link(db, token, "newpwd456")
        assert exc.value.errcode in (
            AuthErr.ACCOUNT_LEVEL_INSUFFICIENT,
            AuthErr.RECOVERY_NOT_SUPPORTED,
        )

    async def should_reset_failed_login_attempts_and_lock(self, db: AsyncSession):
        user = await _mk_normal(
            db, username="bob", password="old123", email="bob@example.com"
        )
        user.failed_login_attempts = 5
        user.is_locked = True
        user.locked_until = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
        await db.flush()

        token = await _create_magic_link(db, "bob@example.com", "reset")
        await _svc().recover_by_magic_link(db, token, "newpwd456")

        await db.refresh(user)
        assert user.failed_login_attempts == 0
        assert user.is_locked is False
        assert user.locked_until is None

    async def should_revoke_all_refresh_tokens(self, db: AsyncSession):
        import datetime as dt

        user = await _mk_normal(
            db, username="bob", password="old123", email="bob@example.com"
        )
        tok = RefreshToken(
            user_id=user.id,
            token_hash="ghi789",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
        )
        db.add(tok)
        await db.flush()
        assert tok.revoked_at is None

        token = await _create_magic_link(db, "bob@example.com", "reset")
        await _svc().recover_by_magic_link(db, token, "newpwd456")

        await db.refresh(tok)
        assert tok.revoked_at is not None

    async def should_reject_expired_token(self, db: AsyncSession):
        import datetime as dt
        import secrets

        await _mk_normal(db, username="bob", password="old123", email="bob@example.com")

        raw = secrets.token_hex(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        link = MagicLink(
            email="bob@example.com",
            token_hash=token_hash,
            purpose="reset",
            expires_at=expires,
        )
        db.add(link)
        await db.flush()

        with pytest.raises(BizError) as exc:
            await _svc().recover_by_magic_link(db, raw, "newpwd456")
        assert exc.value.errcode == AuthErr.TOKEN_EXPIRED


# ===================================================================
# _find_user_by_contact edge cases
# ===================================================================


class TestFindUserByContact:
    async def should_raise_user_not_found_when_no_match(self, db: AsyncSession):
        with pytest.raises(BizError) as exc:
            from app.modules.auth.service_recovery import find_user_by_contact

            await find_user_by_contact(db, "email", "noone@example.com")
        assert exc.value.errcode == AuthErr.USER_NOT_FOUND

    async def should_raise_recovery_not_supported_for_local_user(
        self, db: AsyncSession
    ):
        await _mk_local(db, username="alice")
        user = cast(
            User,
            (await db.execute(select(User).where(User.username == "alice")))
            .scalars()
            .first(),
        )
        user.email = "alice@example.com"
        await db.flush()

        with pytest.raises(BizError) as exc:
            from app.modules.auth.service_recovery import find_user_by_contact

            await find_user_by_contact(db, "email", "alice@example.com")
        assert exc.value.errcode == AuthErr.RECOVERY_NOT_SUPPORTED
