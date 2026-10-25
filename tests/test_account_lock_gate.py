"""P0c 账号锁定门禁：统一判据、永久锁生效、所有发令牌入口兜底。

回归背景（改动前）：
- 登录 / token 依赖 / refresh / authz 裁决 / admin 会话各自内联
  ``is_locked and locked_until and locked_until > now``，**永久锁**（``locked_until=NULL``）
  被当成未锁定放行；
- passkey 登录直接 ``finalize_auth_response``，整条路径没有查锁。

现在判据统一在 ``auth.account_state.lock_is_active``，并在
``finalize_auth_response``（所有发令牌入口的汇合点）加兜底门禁。
"""

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from auth.account_state import clear_expired_lock, lock_is_active
from auth.errors import AuthErr
from auth.models import Profile, User
from auth.schemas import UserLoginPassword
from auth.security import hashpwd
from auth.service_auth import (
    finalize_auth_response,
    issue_session_tokens,
    login_password,
    refresh_access_token,
    store_refresh_token,
)
from auth.service_authz import authorize_user
from core.db.base import now_iso
from core.err import BizError

_PASSWORD = "secret123456"


async def _mk_user(
    db: AsyncSession,
    *,
    username: str,
    locked_until: dt.datetime | None = None,
    permanent_lock: bool = False,
) -> User:
    """建一个 auth 用户；可按需设永久锁 / 指定到期时间的临时锁。"""
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd(_PASSWORD),
        is_locked=permanent_lock or locked_until is not None,
        locked_until=locked_until,
        failed_login_attempts=5 if (permanent_lock or locked_until) else 0,
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


def _active_lock(minutes: int = 5) -> dt.datetime:
    return now_iso() + dt.timedelta(minutes=minutes)


def _expired_lock(minutes: int = 5) -> dt.datetime:
    return now_iso() - dt.timedelta(minutes=minutes)


class TestLockSemantics:
    def test_flag_off_is_never_locked(self) -> None:
        assert (
            lock_is_active(User(is_locked=False, locked_until=_active_lock())) is False
        )

    def test_permanent_lock_is_active(self) -> None:
        assert lock_is_active(User(is_locked=True, locked_until=None)) is True

    def test_future_lock_is_active(self) -> None:
        assert lock_is_active(User(is_locked=True, locked_until=_active_lock())) is True

    def test_expired_lock_is_inactive(self) -> None:
        assert (
            lock_is_active(User(is_locked=True, locked_until=_expired_lock())) is False
        )

    def test_clear_expired_lock_keeps_permanent(self) -> None:
        permanent = User(is_locked=True, locked_until=None, failed_login_attempts=5)
        clear_expired_lock(permanent)
        assert permanent.is_locked is True
        assert permanent.failed_login_attempts == 5

    def test_clear_expired_lock_resets_expired(self) -> None:
        expired = User(
            is_locked=True, locked_until=_expired_lock(), failed_login_attempts=5
        )
        clear_expired_lock(expired)
        assert expired.is_locked is False
        assert expired.locked_until is None
        assert expired.failed_login_attempts == 0


class TestLoginGate:
    async def test_permanent_lock_blocks_password_login(
        self, auth_db: AsyncSession
    ) -> None:
        await _mk_user(auth_db, username="perm_login", permanent_lock=True)

        with pytest.raises(BizError) as exc:
            await login_password(
                auth_db,
                UserLoginPassword(account="perm_login", password=_PASSWORD),
            )
        assert exc.value.errcode == AuthErr.INVALID_CREDENTIALS

    async def test_active_timed_lock_blocks_password_login(
        self, auth_db: AsyncSession
    ) -> None:
        await _mk_user(auth_db, username="timed_login", locked_until=_active_lock())

        with pytest.raises(BizError) as exc:
            await login_password(
                auth_db,
                UserLoginPassword(account="timed_login", password=_PASSWORD),
            )
        assert exc.value.errcode == AuthErr.INVALID_CREDENTIALS

    async def test_expired_lock_auto_unlocks_and_login_succeeds(
        self, auth_db: AsyncSession
    ) -> None:
        user = await _mk_user(
            auth_db, username="expired_login", locked_until=_expired_lock()
        )

        result = await login_password(
            auth_db,
            UserLoginPassword(account="expired_login", password=_PASSWORD),
        )

        assert result["user_id"] == user.id
        assert user.is_locked is False
        assert user.locked_until is None
        assert user.failed_login_attempts == 0


class TestTokenIssuanceGate:
    async def test_permanent_lock_blocks_finalize_for_passkey(
        self, auth_db: AsyncSession
    ) -> None:
        """passkey 登录只经 finalize_auth_response 发 token，必须被兜底门禁拦住。"""
        user = await _mk_user(auth_db, username="perm_passkey", permanent_lock=True)

        with pytest.raises(BizError) as exc:
            await finalize_auth_response(auth_db, user)
        assert exc.value.errcode == AuthErr.INVALID_CREDENTIALS

    async def test_permanent_lock_blocks_token_authorization(
        self, auth_db: AsyncSession
    ) -> None:
        """token 依赖 / seam 裁决对永久锁必须判 locked。"""
        user = await _mk_user(auth_db, username="perm_token", permanent_lock=True)

        verdict = await authorize_user(
            auth_db,
            user_id=user.id,
            expect_token_version=user.token_version,
            iat_ts=None,
            require_admin=False,
        )

        assert verdict["ok"] is False
        assert verdict["cause"] == "locked"

    async def test_permanent_lock_blocks_refresh(self, auth_db: AsyncSession) -> None:
        user = await _mk_user(auth_db, username="perm_refresh", permanent_lock=True)
        raw = "r" * 64
        await store_refresh_token(auth_db, user.id, raw)

        with pytest.raises(BizError) as exc:
            await refresh_access_token(auth_db, raw)
        assert exc.value.errcode == AuthErr.ACCOUNT_LOCKED

    async def test_issue_session_tokens_blocks_locked(
        self, auth_db: AsyncSession
    ) -> None:
        """2FA 完成 / step-up 等直连 issue_session_tokens 的入口也必须被拦。"""
        user = await _mk_user(auth_db, username="perm_issue", permanent_lock=True)

        with pytest.raises(BizError) as exc:
            await issue_session_tokens(auth_db, user)
        assert exc.value.errcode == AuthErr.INVALID_CREDENTIALS
