"""Tests for email/phone binding endpoints (router_settings.py).

Covers:
- Bind email request + verify with upgrade local->normal
- Bind phone request + verify
- Error cases: duplicate email/phone, wrong code
"""

import json
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError, CommonErr
from auth.deps import CurrentUser
from auth.errors import AuthErr
from auth.models import User
from auth.router_settings import BindEmailVerify, BindPhoneVerify
from auth.schemas import UnbindRequest


def _FakeCurrentUser(
    id: int, account_level: str = "local", role: str = "member"
) -> CurrentUser:
    """测试辅助：构造一个满足 ``CurrentUser`` 类型的用户上下文。"""
    return CurrentUser(id=id, account_level=account_level, role=role)


def _unwrap(response: Any) -> dict[str, Any]:
    """Extract the data dict from a JSONResponse returned by @respond."""
    return json.loads(response.body.decode())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# service helpers (mimic test_auth_service pattern)
# ---------------------------------------------------------------------------


async def _reg_local(
    auth_db: AsyncSession, username: str = "alice", password: str = "secret123456"
) -> dict[str, Any]:
    from auth.schemas import UserRegLocal

    svc = _service()
    return await svc.register_local(
        auth_db, UserRegLocal(username=username, password=password)
    )


async def _get_user(auth_db: AsyncSession, user_id: int) -> User:
    from auth.models import User

    # 测试均为“先建后查”，必然命中，返回类型直接按 User 处理
    return cast(
        User,
        (await auth_db.execute(select(User).where(User.id == user_id))).scalars().first(),
    )


def _service():
    from auth import service_auth

    return service_auth


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBindEmail:
    """Bind email request + verify, covering auto-upgrade local->normal."""

    async def should_bind_email_and_upgrade_local_to_normal(self, auth_db: AsyncSession):
        """Full happy path: request code, verify, email bound, account upgraded."""
        # Arrange: create a local user
        reg_result = await _reg_local(auth_db, username="alice")
        user_id = reg_result["user_id"]
        assert (await _get_user(auth_db, user_id)).account_level == "local"

        # Act: request email binding（验证码直接取自 create_email_verification 返回值）
        from auth.service_verify import create_email_verification

        code, _ = await create_email_verification(auth_db, "alice@example.com", "bind")

        # Now use the router function directly
        from auth.router_settings import bind_email_verify

        result = await bind_email_verify(
            body=BindEmailVerify(email="alice@example.com", code=code),
            cur=_FakeCurrentUser(user_id),
            db=auth_db,
        )

        # Assert
        data = _unwrap(result)
        assert data["data"]["message"] == "Email bound successfully"

        user = await _get_user(auth_db, user_id)
        assert user.email == "alice@example.com"
        # account should be upgraded from local to normal
        assert user.account_level == "normal"

    async def should_reject_duplicate_email(self, auth_db: AsyncSession):
        """Should fail if email is already taken by another user."""
        # Create two local users
        reg1 = await _reg_local(auth_db, username="alice")
        reg2 = await _reg_local(auth_db, username="bob")

        from auth.service_verify import create_email_verification

        # Bind email to alice directly
        user1 = await _get_user(auth_db, reg1["user_id"])
        user1.email = "same@example.com"
        await auth_db.flush()

        # Try to bind the same email to bob
        code, _ = await create_email_verification(auth_db, "same@example.com", "bind")

        from auth.router_settings import bind_email_verify

        with pytest.raises(BizError) as exc:
            await bind_email_verify(
                body=BindEmailVerify(email="same@example.com", code=code),
                cur=_FakeCurrentUser(reg2["user_id"]),
                db=auth_db,
            )
        assert exc.value.errcode == AuthErr.ALREADY_REGISTERED

    async def should_reject_wrong_code(self, auth_db: AsyncSession):
        """Should fail with wrong verification code."""
        reg = await _reg_local(auth_db, username="alice")

        from auth.service_verify import create_email_verification

        await create_email_verification(auth_db, "alice@example.com", "bind")

        from auth.router_settings import bind_email_verify

        with pytest.raises(BizError) as exc:
            await bind_email_verify(
                body=BindEmailVerify(email="alice@example.com", code="000000"),
                cur=_FakeCurrentUser(reg["user_id"]),
                db=auth_db,
            )
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID


class TestBindPhone:
    """Bind phone request + verify."""

    async def should_bind_phone_and_upgrade_local_to_normal(self, auth_db: AsyncSession):
        """Full happy path: request code, verify, phone bound, account upgraded."""
        reg = await _reg_local(auth_db, username="alice")
        user_id = reg["user_id"]
        assert (await _get_user(auth_db, user_id)).account_level == "local"

        from auth.service_verify import create_phone_verification

        code, _ = await create_phone_verification(auth_db, "13800001111", "bind")

        from auth.router_settings import bind_phone_verify

        result = await bind_phone_verify(
            body=BindPhoneVerify(phone="13800001111", code=code),
            cur=_FakeCurrentUser(user_id),
            db=auth_db,
        )

        data = _unwrap(result)
        assert data["data"]["message"] == "Phone bound successfully"

        user = await _get_user(auth_db, user_id)
        assert user.phone == "13800001111"
        assert user.account_level == "normal"

    async def should_reject_duplicate_phone(self, auth_db: AsyncSession):
        """Should fail if phone already taken."""
        reg1 = await _reg_local(auth_db, username="alice")
        reg2 = await _reg_local(auth_db, username="bob")

        # Bind phone to alice directly
        user1 = await _get_user(auth_db, reg1["user_id"])
        user1.phone = "13800001111"
        await auth_db.flush()

        from auth.service_verify import create_phone_verification

        code, _ = await create_phone_verification(auth_db, "13800001111", "bind")

        from auth.router_settings import bind_phone_verify

        with pytest.raises(BizError) as exc:
            await bind_phone_verify(
                body=BindPhoneVerify(phone="13800001111", code=code),
                cur=_FakeCurrentUser(reg2["user_id"]),
                db=auth_db,
            )
        assert exc.value.errcode == AuthErr.ALREADY_REGISTERED

    async def should_reject_wrong_code(self, auth_db: AsyncSession):
        """Should fail with wrong verification code."""
        reg = await _reg_local(auth_db, username="alice")

        from auth.service_verify import create_phone_verification

        await create_phone_verification(auth_db, "13800001111", "bind")

        from auth.router_settings import bind_phone_verify

        with pytest.raises(BizError) as exc:
            await bind_phone_verify(
                body=BindPhoneVerify(phone="13800001111", code="000000"),
                cur=_FakeCurrentUser(reg["user_id"]),
                db=auth_db,
            )
        assert exc.value.errcode == AuthErr.VERIFICATION_CODE_INVALID


class TestBindEmailUpgrade:
    """Specifically test that bind email upgrades local->normal."""

    async def should_upgrade_when_binding_email(self, auth_db: AsyncSession):
        """Bind email to a local user: account_level must become normal."""
        reg = await _reg_local(auth_db, username="upgrademe")
        user_id = reg["user_id"]
        user = await _get_user(auth_db, user_id)
        assert user.account_level == "local"
        assert user.email is None

        from auth.service_verify import create_email_verification

        code, _ = await create_email_verification(auth_db, "upgrade@example.com", "bind")

        from auth.router_settings import bind_email_verify

        await bind_email_verify(
            body=BindEmailVerify(email="upgrade@example.com", code=code),
            cur=_FakeCurrentUser(user_id),
            db=auth_db,
        )

        user = await _get_user(auth_db, user_id)
        assert user.email == "upgrade@example.com"
        assert user.account_level == "normal"

    async def should_not_downgrade_normal_user(self, auth_db: AsyncSession):
        """Binding email to an already-normal user: should stay normal."""
        from auth.models import Profile, User

        # Create an already-normal user
        user = User(
            username="normaluser",
            email="normal@example.com",
            hashed_password="x",
            account_level="normal",
        )
        auth_db.add(user)
        await auth_db.flush()
        auth_db.add(Profile(user_id=user.id, role="member"))
        await auth_db.flush()
        user_id = user.id

        # Bind a different email (the user already has one, but we're binding another)
        from auth.service_verify import create_email_verification

        code, _ = await create_email_verification(auth_db, "another@example.com", "bind")

        from auth.router_settings import bind_email_verify

        await bind_email_verify(
            body=BindEmailVerify(email="another@example.com", code=code),
            cur=_FakeCurrentUser(user_id, account_level="normal"),
            db=auth_db,
        )

        user = await _get_user(auth_db, user_id)
        # Already normal, should not have been changed
        assert user.account_level == "normal"


class TestGetSettings:
    """GET /auth/settings — 查询绑定状态。"""

    def _unwrap(self, response: Any) -> dict[str, Any]:
        return json.loads(response.body.decode())

    async def should_return_binding_state(self, auth_db: AsyncSession):
        from auth.models import Profile, User

        user = User(
            username="bindstate",
            email="a@b.com",
            phone="13800001111",
            hashed_password="x",
            account_level="normal",
        )
        auth_db.add(user)
        await auth_db.flush()
        auth_db.add(Profile(user_id=user.id, role="member"))
        await auth_db.flush()

        from auth.models import TOTP

        auth_db.add(TOTP(user_id=user.id, secret="s", enabled=True))
        await auth_db.flush()

        from auth.router_settings import get_settings

        data = self._unwrap(
            await get_settings(
                cur=_FakeCurrentUser(user.id, account_level="normal"), db=auth_db
            )
        )
        assert data["data"]["email"] == "a@b.com"
        assert data["data"]["phone"] == "13800001111"
        assert data["data"]["github"] is None
        assert data["data"]["has_2fa"] is True


class TestUnbind:
    """DELETE /auth/settings/{type} — 解绑 + 2FA 门槛 + 保留一种登录方式。"""

    async def _reg_with_bindings(
        self, auth_db: AsyncSession, email: str = "a@b.com", phone: str = "13800001111"
    ) -> User:
        from auth.models import Profile, User

        user = User(
            username="unbind",
            email=email,
            phone=phone,
            hashed_password="x",
            account_level="normal",
        )
        auth_db.add(user)
        await auth_db.flush()
        auth_db.add(Profile(user_id=user.id, role="member"))
        await auth_db.flush()
        return user

    async def should_unbind_email_without_2fa(self, auth_db: AsyncSession):
        from auth.models import User

        user = await self._reg_with_bindings(auth_db)
        from auth.router_settings import unbind

        data = _unwrap(
            await unbind(
                "email",
                UnbindRequest(code=None),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        )
        assert data["data"]["message"] == "email unbound"
        # 直接用标量列查询，避免命中身份映射中已过期的 User 对象触发惰性加载
        assert await auth_db.scalar(select(User.email).where(User.id == user.id)) is None

    async def should_reject_unbind_when_only_one_way_left(self, auth_db: AsyncSession):
        # 只有 phone，没有 email/github → 解绑 email 会触发“保留一种”守卫（虽然 email 本来就空，走 phone 侧测试更贴）
        from auth.models import Profile, User

        user = User(
            username="onlyphone",
            phone="13800009999",
            hashed_password="x",
            account_level="normal",
        )
        auth_db.add(user)
        await auth_db.flush()
        auth_db.add(Profile(user_id=user.id, role="member"))
        await auth_db.flush()
        # 绑定另一个联系方式以便 email 存在可解绑，但仅剩 phone 时会拒绝
        user.email = "a@b.com"
        await auth_db.flush()

        from app.core.err import BizError
        from auth.router_settings import unbind

        # 先解绑 phone，使仅剩 email
        _unwrap(
            await unbind(
                "phone",
                UnbindRequest(code=None),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        )
        # 再解绑 email，将无任何登录方式 → 应拒绝
        with pytest.raises(BizError) as exc:
            await unbind(
                "email",
                UnbindRequest(code=None),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        assert exc.value.errcode == CommonErr.INVALID_INPUT

    async def should_require_totp_when_2fa_enabled(self, auth_db: AsyncSession):
        user = await self._reg_with_bindings(auth_db)
        from auth.models import TOTP

        auth_db.add(TOTP(user_id=user.id, secret="s", enabled=True))
        await auth_db.flush()

        from app.core.err import BizError
        from auth.router_settings import unbind

        with pytest.raises(BizError) as exc:
            await unbind(
                "email",
                UnbindRequest(code=None),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        assert exc.value.errcode == AuthErr.TOTP_CODE_INVALID

    async def should_unbind_github(self, auth_db: AsyncSession):
        user = await self._reg_with_bindings(auth_db)
        from auth.models import UserOAuth

        auth_db.add(
            UserOAuth(
                user_id=user.id,
                provider="github",
                provider_user_id="123",
                provider_email="gh@example.com",
            )
        )
        await auth_db.flush()
        from auth.router_settings import unbind

        data = _unwrap(
            await unbind(
                "github",
                UnbindRequest(code=None),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        )
        assert data["data"]["message"] == "github unbound"
        # 用标量列查询判定行已删除，绕过过期身份映射对象
        assert (
            await auth_db.execute(select(UserOAuth.id).where(UserOAuth.user_id == user.id))
        ).scalars().first() is None

    async def should_require_2fa_for_github_unbind(self, auth_db: AsyncSession):
        """解绑 GitHub 已开启 2FA 时，同样要求二次验证（TOTP 或恢复码）。"""
        from auth.models import TOTP

        user = await self._reg_with_bindings(auth_db)
        auth_db.add(TOTP(user_id=user.id, secret="s", enabled=True))
        await auth_db.flush()
        from auth.router_settings import unbind

        with pytest.raises(BizError) as exc:
            await unbind(
                "github",
                UnbindRequest(code=None),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        assert exc.value.errcode == AuthErr.TOTP_CODE_INVALID

    async def should_unbind_github_with_recovery_code(self, auth_db: AsyncSession):
        """解绑 GitHub 已开启 2FA 时，可用合法恢复码兜底完成。"""
        import hashlib

        from auth.models import TOTP, RecoveryCode, UserOAuth
        from auth.router_settings import unbind

        user = await self._reg_with_bindings(auth_db)
        auth_db.add(TOTP(user_id=user.id, secret="s", enabled=True))
        auth_db.add(
            UserOAuth(
                user_id=user.id,
                provider="github",
                provider_user_id="123",
                provider_email="gh@example.com",
            )
        )
        auth_db.add(
            RecoveryCode(
                user_id=user.id,
                code_hash=hashlib.sha256(b"rc-gh-1").hexdigest(),
                used=False,
            )
        )
        await auth_db.flush()

        data = _unwrap(
            await unbind(
                "github",
                UnbindRequest(code=None, recovery_code="rc-gh-1"),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        )
        assert data["data"]["message"] == "github unbound"
        assert (
            await auth_db.execute(select(UserOAuth.id).where(UserOAuth.user_id == user.id))
        ).scalars().first() is None

    async def should_reject_invalid_type(self, auth_db: AsyncSession):
        user = await self._reg_with_bindings(auth_db)
        from app.core.err import BizError
        from auth.router_settings import unbind

        with pytest.raises(BizError) as exc:
            await unbind(
                "wechat",
                UnbindRequest(code=None),
                cur=_FakeCurrentUser(user.id, account_level="normal"),
                db=auth_db,
            )
        assert exc.value.errcode == CommonErr.INVALID_INPUT
