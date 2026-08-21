import datetime
import hashlib
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.db.models import User, expires_at
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import RefreshToken

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _reg_local(
    db: AsyncSession, username: str = "alice", password: str = "secret123456"
) -> dict[str, Any]:
    from app.modules.auth.schemas import UserRegLocal

    return await _service().register_local(
        db, UserRegLocal(username=username, password=password)
    )


async def _reg_normal(
    db: AsyncSession,
    username: str = "bob",
    password: str = "secret123456",
    email: str = "bob@example.com",
    phone: str = "13800001111",
) -> dict[str, Any]:
    """创建带邮箱/手机号的 normal 账户（测试辅助）。

    走 ``register_local`` + 手动补充 contact，避免依赖已过时的一次性
    ``register_normal_with_password`` 注册 API。
    """
    from app.db.models import User
    from app.modules.auth.schemas import UserRegLocal

    result = await _service().register_local(
        db, UserRegLocal(username=username, password=password)
    )
    user = await _get(db, User, User.id == result["user_id"])
    if email is not None:
        user.email = email
    if phone is not None:
        user.phone = phone
    if email or phone:
        user.account_level = "normal"
    await db.flush()
    return result


async def _login(db: AsyncSession, account: str, password: str) -> dict[str, Any]:
    from app.modules.auth.schemas import UserLoginPassword

    return await _service().login_password(
        db, UserLoginPassword(account=account, password=password)
    )


def _service():
    from app.modules.auth import service_auth

    return service_auth


async def _get[T](db: AsyncSession, model: type[T], *where: Any) -> T:
    # 测试均为“先建后查”，必然命中，返回类型直接按 _T 处理
    return cast(T, (await db.execute(select(model).where(*where))).scalars().first())


# ===================================================================
# TestRegisterLocal
# ===================================================================


class TestRegisterLocal:
    async def should_create_local_user_and_profile(self, db: AsyncSession):
        from app.db.models import Profile, User

        result = await _reg_local(db, username="alice", password="secret123456")
        assert result["access_token"]
        assert result["refresh_token"]
        assert result["user_id"] == 1
        assert result["account_level"] == "local"

        user = await _get(db, User, User.id == 1)
        assert user.username == "alice"
        assert user.account_level == "local"
        assert "$" in user.hashed_password

        profile = await _get(db, Profile, Profile.user_id == 1)
        assert profile is not None
        assert profile.role == "member"

    async def should_reject_duplicate_username(self, db: AsyncSession):
        await _reg_local(db, username="alice")
        with pytest.raises(BizError) as exc:
            await _reg_local(db, username="alice", password="other1234567")
        assert exc.value.errcode == AuthErr.ALREADY_REGISTERED


# ===================================================================
# TestLoginPassword
# ===================================================================


class TestLoginPassword:
    # --- success paths ---

    async def should_login_by_username(self, db: AsyncSession):
        await _reg_local(db, username="alice", password="secret123456")
        result = await _login(db, "alice", "secret123456")
        assert result["user_id"] == 1
        assert result["access_token"]
        assert result["refresh_token"]
        assert result["account_level"] == "local"

    async def should_login_by_email(self, db: AsyncSession):
        from app.db.models import User

        await _reg_local(db, username="alice", password="secret123456")
        # give the user an email manually
        user = await _get(db, User, User.id == 1)
        user.email = "alice@example.com"
        await db.flush()

        result = await _login(db, "alice@example.com", "secret123456")
        assert result["user_id"] == 1

    async def should_login_by_phone(self, db: AsyncSession):
        from app.db.models import User

        await _reg_local(db, username="alice", password="secret123456")
        user = await _get(db, User, User.id == 1)
        user.phone = "13800001111"
        await db.flush()

        result = await _login(db, "13800001111", "secret123456")
        assert result["user_id"] == 1

    # --- failure paths ---

    async def should_reject_wrong_password(self, db: AsyncSession):
        await _reg_local(db, username="alice", password="secret123456")
        with pytest.raises(BizError) as exc:
            await _login(db, "alice", "wrongpass")
        assert exc.value.errcode == AuthErr.INVALID_CREDENTIALS

    async def should_reject_nonexistent_account(self, db: AsyncSession):
        with pytest.raises(BizError) as exc:
            await _login(db, "nobody", "secret123456")
        assert exc.value.errcode == AuthErr.INVALID_CREDENTIALS

    async def should_lock_after_5_failed_attempts(self, db: AsyncSession):
        from app.db.models import User

        await _reg_local(db, username="alice", password="secret123456")
        for _ in range(5):
            try:
                await _login(db, "alice", "wrongpass")
            except BizError:
                # Expire only – don't rollback, so the flush in _record_failed_attempt
                # is preserved. The exception itself rolls back nothing at the DB level
                # because autoflush=False; only flush calls persist.
                db.expire_all()

        user = await _get(db, User, User.username == "alice")
        assert user.failed_login_attempts == 5
        assert user.is_locked is True

        with pytest.raises(BizError) as exc:
            await _login(db, "alice", "secret123456")
        assert exc.value.errcode == AuthErr.INVALID_CREDENTIALS

    async def should_reset_failed_counter_on_success(self, db: AsyncSession):
        from app.db.models import User

        await _reg_local(db, username="alice", password="secret123456")
        # 2 failures
        for _ in range(2):
            try:
                await _login(db, "alice", "wrongpass")
            except BizError:
                db.expire_all()

        # then success
        result = await _login(db, "alice", "secret123456")
        assert result["user_id"] == 1

        user = await _get(db, User, User.username == "alice")
        assert user.failed_login_attempts == 0
        assert user.is_locked is False

    async def should_return_setup_token_for_admin_without_totp(self, db: AsyncSession):
        """Admin-level user without TOTP should get a setup_required response."""
        from app.db.models import User

        user = User(
            username="admin",
            email="admin@example.com",
            hashed_password="dummy$notreal",
            account_level="admin",
        )
        db.add(user)
        await db.flush()
        from app.modules.auth.security import hashpwd

        user.hashed_password = await hashpwd("admin123")
        await db.flush()

        result = await _login(db, "admin", "admin123")
        assert result["requires_2fa"] is True
        assert result.get("setup_required") is True
        assert result["temp_token"] is not None

    async def should_login_with_totp_without_forced_2fa(self, db: AsyncSession):
        """登录不再强制 2FA：已启用 TOTP 的普通用户直接得完整会话令牌（危险操作时才 step-up）。"""
        from app.db.models import User
        from app.modules.auth.models import TOTP

        user = User(
            username="secure",
            email="secure@example.com",
            hashed_password="dummy$notreal",
            account_level="normal",
        )
        db.add(user)
        await db.flush()
        from app.modules.auth.security import hashpwd

        user.hashed_password = await hashpwd("secret123456")

        # enable TOTP
        totp = TOTP(user_id=user.id, secret="MZXW6YTBOJQXI33F", enabled=True)
        db.add(totp)
        await db.flush()

        result = await _login(db, "secure", "secret123456")
        assert result["requires_2fa"] is False
        assert result["temp_token"] is None
        assert result["access_token"] is not None
        assert result["refresh_token"] is not None


# ===================================================================
# TestRegisterByVerify
# ===================================================================


class TestRegisterByVerify:
    async def should_create_user_by_email_verify(self, db: AsyncSession):
        from app.db.models import User

        svc = _service()
        result = await svc.register_by_verify(db, "email", "new@example.com")
        user_id = result["user_id"]
        assert result["access_token"] is not None
        assert result["refresh_token"] is not None
        assert result["account_level"] == "normal"

        user = await _get(db, User, User.id == user_id)
        assert user.email == "new@example.com"
        assert user.account_level == "normal"
        assert user.hashed_password == ""

    async def should_create_user_by_phone_verify(self, db: AsyncSession):
        from app.db.models import User

        svc = _service()
        result = await svc.register_by_verify(db, "phone", "13900001111")
        user_id = result["user_id"]
        assert result["access_token"] is not None

        user = await _get(db, User, User.id == user_id)
        assert user.phone == "13900001111"
        assert user.account_level == "normal"


# ===================================================================
# TestUpgrade
# ===================================================================


class TestUpgrade:
    async def should_upgrade_local_to_normal(self, db: AsyncSession):
        from app.db.models import User

        await _reg_local(db, username="alice")
        user = await _get(db, User, User.username == "alice")
        assert user.account_level == "local"

        svc = _service()
        await svc.upgrade_to_normal(db, user)
        await db.flush()

        user = await _get(db, User, User.username == "alice")
        assert user.account_level == "normal"

    async def should_not_downgrade_already_normal(self, db: AsyncSession):
        from app.db.models import User

        user = User(
            username="normal_guy",
            email="n@example.com",
            hashed_password="x",
            account_level="normal",
        )
        db.add(user)
        await db.flush()

        svc = _service()
        await svc.upgrade_to_normal(db, user)
        await db.flush()

        assert user.account_level == "normal"

    async def admin_should_stay_admin(self, db: AsyncSession):
        from app.db.models import User

        user = User(
            username="boss",
            email="boss@example.com",
            hashed_password="x",
            account_level="admin",
        )
        db.add(user)
        await db.flush()

        svc = _service()
        await svc.upgrade_to_normal(db, user)
        await db.flush()

        assert user.account_level == "admin"


# ===================================================================
# TestRefresh
# ===================================================================


class TestRefresh:
    async def should_refresh_valid_token(self, db: AsyncSession):
        await _reg_local(db, username="alice", password="secret123456")
        tokens = (
            (await db.execute(select(RefreshToken).where(RefreshToken.user_id == 1)))
            .scalars()
            .all()
        )
        assert len(tokens) == 1

        # get the raw refresh token from the registration result
        result = await _reg_local(db, username="bob", password="other1234567")
        raw = result["refresh_token"]

        svc = _service()
        new = await svc.refresh_access_token(db, raw)
        assert new["access_token"]
        assert new["refresh_token"]
        assert new["refresh_token"] != raw  # rotation

        # old token should be revoked
        old_hash = hashlib.sha256(raw.encode()).hexdigest()
        old = await _get(db, RefreshToken, RefreshToken.token_hash == old_hash)
        assert old.revoked_at is not None

    async def should_inherit_stepup_mfa_trust_on_refresh(self, db: AsyncSession):
        """前台 step-up 2FA 信任（mfa_at 原点）应随刷新轮换继承，1h 窗口不被 15min access 轮换重置。"""
        from app.modules.auth.security import decode_access_token
        from app.modules.auth.service_auth import issue_session_tokens

        await _reg_local(db, username="alice", password="secret123456")
        user = (await db.execute(select(User).where(User.username == "alice"))).scalars().first()
        assert user is not None

        _, raw = await issue_session_tokens(db, user, mfa_verified=True)
        svc = _service()
        new = await svc.refresh_access_token(db, raw)
        payload = decode_access_token(new["access_token"])
        assert payload["mfa"] is True
        assert payload["mfa_at"] is not None
        # 刷新不延长信任：新 access token 的 mfa_at 应仍是不晚于当前时刻（保留原点，而非 now）
        assert payload["mfa_at"] <= int(datetime.datetime.now(datetime.UTC).timestamp()) + 2

        # 未做 step-up 的普通会话刷新后仍无 mfa 标记
        result = await _reg_local(db, username="bob", password="other1234567")
        new_plain = await svc.refresh_access_token(db, result["refresh_token"])
        assert decode_access_token(new_plain["access_token"])["mfa"] is not True

    async def should_reject_revoked_token(self, db: AsyncSession):
        result = await _reg_local(db, username="alice")
        raw = result["refresh_token"]

        svc = _service()
        # first refresh works
        await svc.refresh_access_token(db, raw)

        # second refresh with same (revoked) token rejects
        with pytest.raises(BizError) as exc:
            await svc.refresh_access_token(db, raw)
        assert exc.value.errcode == AuthErr.TOKEN_INVALID

    async def should_reject_expired_token(self, db: AsyncSession):
        result = await _reg_local(db, username="alice")
        raw = result["refresh_token"]

        # manually expire the token in DB
        tok_hash = hashlib.sha256(raw.encode()).hexdigest()

        tok = await _get(db, RefreshToken, RefreshToken.token_hash == tok_hash)
        tok.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            days=1
        )
        await db.flush()

        svc = _service()
        with pytest.raises(BizError) as exc:
            await svc.refresh_access_token(db, raw)
        assert exc.value.errcode == AuthErr.TOKEN_EXPIRED


# ===================================================================
# TestRevokeAll
# ===================================================================


class TestRevokeAll:
    async def should_revoke_all_user_tokens(self, db: AsyncSession):
        await _reg_local(db, username="alice")
        await _reg_local(db, username="bob")

        svc = _service()
        await svc.revoke_all_refresh_tokens(db, 1)

        # alice's tokens revoked
        tok1 = (
            (await db.execute(select(RefreshToken).where(RefreshToken.user_id == 1)))
            .scalars()
            .all()
        )
        for t in tok1:
            assert t.revoked_at is not None

        # bob's tokens untouched
        tok2 = (
            (await db.execute(select(RefreshToken).where(RefreshToken.user_id == 2)))
            .scalars()
            .all()
        )
        for t in tok2:
            assert t.revoked_at is None


# ===================================================================
# TestAuditLog
# ===================================================================


class TestAuditLog:
    async def should_create_audit_log(self, db: AsyncSession):
        from app.modules.auth.models import AuditLog

        await _reg_local(db, username="alice")
        svc = _service()
        await svc.log_audit(
            db, 1, "login", detail="password login", ip_address="127.0.0.1"
        )

        logs = (
            (await db.execute(select(AuditLog).where(AuditLog.user_id == 1)))
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].action == "login"
        assert logs[0].detail == "password login"
        assert logs[0].ip_address == "127.0.0.1"


# ===================================================================
# TestRefreshKindIsolation
# ===================================================================


class TestRefreshKindIsolation:
    async def should_reject_admin_kind_refresh_in_web_refresh(self, db: AsyncSession):
        from app.modules.auth.models import RefreshToken
        from app.modules.auth.service_auth import (
            hash_refresh_token,
            refresh_access_token,
        )

        db.add(
            RefreshToken(
                user_id=1,
                token_hash=hash_refresh_token("raw-admin-refresh"),
                kind="admin",
                mfa_verified=True,
                expires_at=expires_at(days=1),
                revoked_at=None,
            )
        )
        await db.flush()

        with pytest.raises(BizError):
            await refresh_access_token(db, "raw-admin-refresh")


class TestEmailCase:
    """邮箱大小写绝对敏感回归测试：存储保留原值，大小写不同是独立账号。"""

    async def should_store_and_match_exact_email_case(self, db: AsyncSession):
        from app.db.models import User
        from app.modules.auth.channels import EMAIL_CHANNEL
        from app.modules.auth.service_auth import _normalize_email

        # 邮箱存储保留原值大小写
        await _reg_normal(
            db, username="bob", email="Mixed.Case@Example.com", phone="13800001111"
        )
        await db.flush()
        stored = (
            (await db.execute(select(User).where(User.username == "bob")))
            .scalars()
            .one()
        )
        assert stored.email == "Mixed.Case@Example.com"

        # 大小写完全一致才查到；不同大小写查不到（严格区分）
        exact = await EMAIL_CHANNEL.find_user(db, "Mixed.Case@Example.com")
        assert exact is not None and exact.id == stored.id
        other_case = await EMAIL_CHANNEL.find_user(db, "MIXED.CASE@EXAMPLE.COM")
        assert other_case is None

        # _normalize_email 只去空白、保留大小写
        assert _normalize_email("  Alice@Ex.Com ") == "Alice@Ex.Com"

    async def should_treat_different_case_as_separate_accounts(self, db: AsyncSession):
        """大小写不同的邮箱是两个独立、互不混淆的账号。"""
        from app.db.models import User
        from app.modules.auth.channels import EMAIL_CHANNEL

        await _reg_normal(
            db, username="bob", email="Bob@Example.com", phone="13800001111"
        )
        await _reg_normal(
            db,
            username="carol",
            email="bob@example.com",
            phone="13800002222",
        )
        await db.flush()

        bob = (
            (await db.execute(select(User).where(User.username == "bob")))
            .scalars()
            .one()
        )
        carol = (
            (await db.execute(select(User).where(User.username == "carol")))
            .scalars()
            .one()
        )
        assert bob.email == "Bob@Example.com"
        assert carol.email == "bob@example.com"
        assert bob.id != carol.id

        # 精确邮箱只命中对应账号
        found_bob = await EMAIL_CHANNEL.find_user(db, "Bob@Example.com")
        found_carol = await EMAIL_CHANNEL.find_user(db, "bob@example.com")
        assert found_bob is not None and found_bob.id == bob.id
        assert found_carol is not None and found_carol.id == carol.id
