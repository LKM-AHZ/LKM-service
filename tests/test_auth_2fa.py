"""Tests for TOTP 2FA service (service_2fa.py).

Covers:
- setup_2fa_begin: rejects local users, generates valid secrets/URIs,
  rejects already-enabled TOTP.
- setup_2fa_complete: verifies code, produces recovery codes.
- verify_2fa: with TOTP code and recovery code.
- disable_2fa: disables TOTP, admin downgrade to normal.
"""

import hashlib
import time
import uuid
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.auth.deps import CurrentUser
from app.modules.auth.errors import AuthErr
from app.modules.auth.models import TOTP, RecoveryCode, User
from app.modules.auth.security import (
    create_access_token,
    create_temp_token,
    decode_access_token,
    encrypt_secret,
    generate_totp_secret,
    hashpwd,
)


@pytest.fixture
async def db(auth_db: AsyncSession) -> AsyncSession:
    """2FA/TOTP/User 表在 auth 独立库（S5 拆后）；本文件默认走 auth 面。"""
    return auth_db


@pytest.fixture
async def fused_front_client(fused_db_session: AsyncSession) -> Any:
    """前台业务路由 + auth 身份同 fused schema 的 HTTP 客户端。

    业务删除路由要读业务库 ``role_permissions``/``content_items``，同时要 auth 身份裁决；
    仅 override auth 会话（``auth_front_client``）会让业务会话落默认库而表不存在。这里把
    ``get_session``/``get_read_session``/``get_auth_session`` 三处都指到 fused（auth+业务
    同 schema），供需要业务权限表的前台用例使用。"""
    from httpx import ASGITransport, AsyncClient

    from app.db.auth_session import get_auth_session
    from app.db.session import get_read_session, get_session
    from app.main import app

    async def _override():
        yield fused_db_session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_read_session] = _override
    app.dependency_overrides[get_auth_session] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_read_session, None)
        app.dependency_overrides.pop(get_auth_session, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _svc():
    from app.modules.auth import service_2fa

    return service_2fa


def _FakeCurrentUser(
    id: uuid.UUID, account_level: str = "local", role: str = "member"
) -> CurrentUser:
    """测试辅助：构造一个满足 ``CurrentUser`` 类型的用户上下文。"""
    return CurrentUser(id=id, account_level=account_level, role=role)


async def _create_user(
    db: AsyncSession,
    username: str = "testuser",
    account_level: str = "normal",
    email: str | None = "test@example.com",
) -> User:
    """Create a minimal user (with profile) and return it."""
    from app.modules.auth.models import Profile

    user = User(
        username=username,
        email=email,
        hashed_password=await hashpwd("secret123456"),
        account_level=account_level,
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member"))
    await db.flush()
    return user


async def _enable_totp_for_user(db: AsyncSession, user_id: uuid.UUID) -> str:
    """Create a TOTP record with a known secret and mark enabled=True."""
    secret = generate_totp_secret()
    encrypted = encrypt_secret(secret)
    db.add(TOTP(user_id=user_id, secret=encrypted, enabled=True))
    await db.flush()
    return secret


def _generate_totp_code(secret: str, offset: int = 0) -> str:
    """Generate a valid TOTP code for *secret* at the current time step + offset."""
    import base64
    import hmac
    import struct

    now = int(time.time()) // 30 + offset
    key = base64.b32decode(secret, casefold=True)
    msg = struct.pack(">Q", now)
    h_val = hmac.new(key, msg, hashlib.sha1).digest()
    off = h_val[-1] & 0x0F
    code = (struct.unpack(">I", h_val[off : off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


async def _get[T](db: AsyncSession, model: type[T], *where: Any) -> T:
    # 测试均为“先建后查”，必然命中，返回类型直接按 _T 处理
    return cast(T, (await db.execute(select(model).where(*where))).scalars().first())


# ===================================================================
# TestSetup2FABegin
# ===================================================================


class TestSetup2FABegin:
    async def should_reject_local_user(self, db: AsyncSession):
        user = await _create_user(
            db, username="localuser", account_level="local", email=None
        )
        with pytest.raises(BizError) as exc:
            await _svc().setup_2fa_begin(db, user.id)
        assert exc.value.errcode == AuthErr.ACCOUNT_LEVEL_INSUFFICIENT

    async def should_return_secret_and_uri_for_normal_user(self, db: AsyncSession):
        user = await _create_user(db, username="normaluser")
        result = await _svc().setup_2fa_begin(db, user.id)
        assert "secret" in result
        assert "qr_code_uri" in result
        assert len(result["secret"]) >= 16
        assert result["qr_code_uri"].startswith("otpauth://totp/")
        assert "normaluser" in result["qr_code_uri"]

    async def should_reject_already_enabled(self, db: AsyncSession):
        user = await _create_user(db, username="enableduser")
        await _enable_totp_for_user(db, user.id)
        with pytest.raises(BizError) as exc:
            await _svc().setup_2fa_begin(db, user.id)
        assert exc.value.errcode == AuthErr.TOTP_ALREADY_ENABLED

    async def should_store_encrypted_secret(self, db: AsyncSession):
        user = await _create_user(db, username="encryptcheck")
        result = await _svc().setup_2fa_begin(db, user.id)
        totp_record = await _get(db, TOTP, TOTP.user_id == user.id)
        assert totp_record is not None
        assert totp_record.secret != result["secret"]  # encrypted != plain
        assert totp_record.enabled is False

    async def should_accept_admin_user(self, db: AsyncSession):
        user = await _create_user(db, username="adminuser", account_level="admin")
        result = await _svc().setup_2fa_begin(db, user.id)
        assert "secret" in result
        assert "qr_code_uri" in result


# ===================================================================
# TestSetup2FAComplete
# ===================================================================


class TestSetup2FAComplete:
    async def should_enable_totp_and_return_recovery_codes(self, db: AsyncSession):
        user = await _create_user(db, username="completeuser")
        # Begin setup to create the TOTP record
        begin_result = await _svc().setup_2fa_begin(db, user.id)
        secret = begin_result["secret"]

        code = _generate_totp_code(secret)
        result = await _svc().setup_2fa_complete(db, user.id, code)
        assert "recovery_codes" in result
        assert len(result["recovery_codes"]) == 10
        assert result["confirmed_saved_required"] is True

        # Verify TOTP is now enabled
        totp_record = await _get(db, TOTP, TOTP.user_id == user.id)
        assert totp_record.enabled is True

        # Verify recovery codes are stored as hashes
        stored_codes = (
            (
                await db.execute(
                    select(RecoveryCode).where(RecoveryCode.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(stored_codes) == 10
        for rc in stored_codes:
            assert rc.used is False

    async def should_reject_when_no_totp_record_exists(self, db: AsyncSession):
        user = await _create_user(db, username="nototp")
        with pytest.raises(BizError) as exc:
            await _svc().setup_2fa_complete(db, user.id, "123456")
        assert exc.value.errcode == AuthErr.TOTP_NOT_ENABLED

    async def should_reject_invalid_code(self, db: AsyncSession):
        user = await _create_user(db, username="badcode")
        await _svc().setup_2fa_begin(db, user.id)
        with pytest.raises(BizError) as exc:
            await _svc().setup_2fa_complete(db, user.id, "000000")
        assert exc.value.errcode == AuthErr.TOTP_CODE_INVALID

    async def should_reject_already_enabled(self, db: AsyncSession):
        user = await _create_user(db, username="alreadyenabled")
        begin_result = await _svc().setup_2fa_begin(db, user.id)
        code = _generate_totp_code(begin_result["secret"])
        await _svc().setup_2fa_complete(db, user.id, code)

        # Second complete should fail
        with pytest.raises(BizError) as exc:
            await _svc().setup_2fa_complete(db, user.id, code)
        assert exc.value.errcode == AuthErr.TOTP_NOT_ENABLED


# ===================================================================
# TestVerify2FA
# ===================================================================


class TestVerify2FA:
    async def should_verify_with_valid_code(self, db: AsyncSession):
        user = await _create_user(db, username="verifyuser")
        secret = await _enable_totp_for_user(db, user.id)

        temp_token = create_temp_token(user.id)
        code = _generate_totp_code(secret)
        result = await _svc().verify_2fa(db, temp_token, code=code)
        assert "access_token" in result
        assert "refresh_token" in result
        assert result["user_id"] == user.id

    async def should_reject_invalid_code(self, db: AsyncSession):
        user = await _create_user(db, username="badverify")
        await _enable_totp_for_user(db, user.id)

        temp_token = create_temp_token(user.id)
        with pytest.raises(BizError) as exc:
            await _svc().verify_2fa(db, temp_token, code="000000")
        assert exc.value.errcode == AuthErr.TOTP_CODE_INVALID

    async def should_verify_with_recovery_code(self, db: AsyncSession):
        user = await _create_user(db, username="recoveruser")
        _ = await _enable_totp_for_user(
            db, user.id
        )  # TOTP 需启用但验证走 recovery code，secret 不在此用

        # Create a recovery code entry directly
        recovery_plain = "test-recovery-code-12345"
        code_hash = hashlib.sha256(recovery_plain.encode()).hexdigest()
        db.add(RecoveryCode(user_id=user.id, code_hash=code_hash, used=False))
        await db.flush()

        temp_token = create_temp_token(user.id)
        result = await _svc().verify_2fa(db, temp_token, recovery_code=recovery_plain)
        assert "access_token" in result
        assert "refresh_token" in result

        # Verify the recovery code is marked used
        rc = await _get(
            db,
            RecoveryCode,
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == code_hash,
        )
        assert rc.used is True

    async def should_reject_invalid_recovery_code(self, db: AsyncSession):
        user = await _create_user(db, username="badrecover")
        await _enable_totp_for_user(db, user.id)

        temp_token = create_temp_token(user.id)
        with pytest.raises(BizError) as exc:
            await _svc().verify_2fa(db, temp_token, recovery_code="nonexistent")
        assert exc.value.errcode == AuthErr.RECOVERY_CODE_INVALID

    async def should_reject_invalid_temp_token(self, db: AsyncSession):
        with pytest.raises(BizError) as exc:
            await _svc().verify_2fa(db, "invalid-token", code="123456")
        assert exc.value.errcode == AuthErr.TOKEN_INVALID

    async def should_override_trust_device_for_admin(self, db: AsyncSession):
        """Admin users should always have trust_device=False."""
        user = await _create_user(db, username="admintrust", account_level="admin")
        secret = await _enable_totp_for_user(db, user.id)

        temp_token = create_temp_token(user.id)
        code = _generate_totp_code(secret)
        result = await _svc().verify_2fa(db, temp_token, code=code, trust_device=True)
        # trust_device should be False even though we passed True
        assert result["trust_device"] is False


# ===================================================================
# TestDisable2FA
# ===================================================================


class TestDisable2FA:
    async def should_disable_totp_and_clear_data(self, auth_db: AsyncSession):
        user = await _create_user(auth_db, username="disableuser")
        secret = await _enable_totp_for_user(auth_db, user.id)

        # Add a recovery code
        code_hash = hashlib.sha256(b"dummy-recovery").hexdigest()
        auth_db.add(RecoveryCode(user_id=user.id, code_hash=code_hash, used=False))
        await auth_db.flush()

        code = _generate_totp_code(secret)
        result = await _svc().disable_2fa(auth_db, user.id, code)
        assert result["message"] == "2FA disabled"

        totp_record = await _get(auth_db, TOTP, TOTP.user_id == user.id)
        assert totp_record.enabled is False
        assert totp_record.secret == ""

        # Recovery codes should be deleted
        rcs = (
            (
                await auth_db.execute(
                    select(RecoveryCode).where(RecoveryCode.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rcs) == 0

    async def should_reject_when_not_enabled(self, auth_db: AsyncSession):
        user = await _create_user(auth_db, username="notenabled")
        with pytest.raises(BizError) as exc:
            await _svc().disable_2fa(auth_db, user.id, "123456")
        assert exc.value.errcode == AuthErr.TOTP_NOT_ENABLED

    async def should_downgrade_admin_to_normal(self, auth_db: AsyncSession):
        user = await _create_user(auth_db, username="admin2fa", account_level="admin")
        secret = await _enable_totp_for_user(auth_db, user.id)

        code = _generate_totp_code(secret)
        result = await _svc().disable_2fa(auth_db, user.id, code)
        assert result["message"] == "2FA disabled"

        # Verify admin downgrade
        user_id = user.id
        auth_db.expire_all()
        user = await _get(auth_db, User, User.id == user_id)
        assert user.account_level == "normal"

    async def should_reject_invalid_code(self, auth_db: AsyncSession):
        user = await _create_user(auth_db, username="wrongcodedisable")
        await _enable_totp_for_user(auth_db, user.id)

        with pytest.raises(BizError) as exc:
            await _svc().disable_2fa(auth_db, user.id, "000000")
        assert exc.value.errcode == AuthErr.TOTP_CODE_INVALID


class TestGet2FAStatus:
    """GET /auth/2fa/status — 查询 2FA 是否已开启。"""

    async def _unwrap(self, response: Any) -> dict[str, Any]:
        import json

        return json.loads(response.body.decode())

    async def should_return_false_when_not_enabled(self, auth_db: AsyncSession):
        user = await _create_user(auth_db, username="statusoff")
        from app.modules.auth.router_2fa import get_2fa_status

        data = await self._unwrap(
            await get_2fa_status(
                cur=_FakeCurrentUser(user.id, account_level="normal"), db=auth_db
            )
        )
        assert data["data"] == {"enabled": False}

    async def should_return_true_when_enabled(self, auth_db: AsyncSession):
        user = await _create_user(auth_db, username="statuson")
        await _enable_totp_for_user(auth_db, user.id)
        from app.modules.auth.router_2fa import get_2fa_status

        data = await self._unwrap(
            await get_2fa_status(
                cur=_FakeCurrentUser(user.id, account_level="normal"), db=auth_db
            )
        )
        assert data["data"] == {"enabled": True}


# ===================================================================
# 管理员强制设置 2FA 后签发 token：role 必须从 profile 读取，而非硬编码 admin
# （复用 issue_session_tokens，见 router_2fa.setup_2fa_complete_temp）
# ===================================================================


class TestIssueAdminSetupTokens:
    async def should_read_role_from_profile(self, db: AsyncSession):
        from app.modules.auth.models import Profile
        from app.modules.auth.security import decode_access_token
        from app.modules.auth.service_auth import issue_session_tokens

        user = await _create_user(db, username="role_admin", account_level="admin")
        profile = await _get(db, Profile, Profile.user_id == user.id)
        profile.role = "admin"
        await db.flush()

        access_token, _ = await issue_session_tokens(db, user, mfa_verified=True)
        assert decode_access_token(access_token)["role"] == "admin"

    async def should_not_hardcode_admin_role(self, db: AsyncSession):
        from app.modules.auth.security import decode_access_token
        from app.modules.auth.service_auth import issue_session_tokens

        # _create_user 的 profile.role 固定为 "member"，即使 account_level=admin
        user = await _create_user(db, username="role_member", account_level="admin")
        access_token, _ = await issue_session_tokens(db, user, mfa_verified=True)
        # 应从 profile 读取得到 "member"，而非硬编码 "admin"
        assert decode_access_token(access_token)["role"] == "member"


# ===================================================================
# 前台危险操作 step-up 2FA：POST /auth/2fa/step-up + require_2fa 删除门禁
# ===================================================================


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestStepUp2FA:
    """POST /auth/2fa/step-up —— 已验证会话基础上补验 TOTP，签发带 mfa 信任的新 access token。

    S5 收敛后为前台认证语义端点：走 :ref:`auth_front_client`(monolith + auth 库会话) 并把用户
    建在 auth 独立库 schema(auth_db) —— 证明 auth router/deps 已绑 ``get_auth_session``。
    """

    async def should_reject_wrong_code(
        self, auth_front_client: Any, auth_db: AsyncSession
    ):
        user = await _create_user(auth_db, username="stepup_bad")
        await _enable_totp_for_user(auth_db, user.id)
        token = create_access_token(
            user_id=user.id, account_level="normal", role="member"
        )
        resp = await auth_front_client.post(
            "/api/v1/auth/2fa/step-up", headers=_auth(token), json={"code": "000000"}
        )
        # verify_user_totp 失败抛 TOTP_CODE_INVALID -> HTTP 400
        assert resp.status_code == 400
        assert resp.json()["code"] != 0

    async def should_issue_mfa_token_on_valid_code(
        self, auth_front_client: Any, auth_db: AsyncSession
    ):
        user = await _create_user(auth_db, username="stepup_ok")
        secret = await _enable_totp_for_user(auth_db, user.id)
        token = create_access_token(
            user_id=user.id, account_level="normal", role="member"
        )
        code = _generate_totp_code(secret)
        resp = await auth_front_client.post(
            "/api/v1/auth/2fa/step-up", headers=_auth(token), json={"code": code}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        payload = decode_access_token(body["data"]["access_token"])
        assert payload["mfa"] is True
        assert payload["mfa_at"] is not None

    async def should_accept_recovery_code(
        self, auth_front_client: Any, auth_db: AsyncSession
    ):
        """step-up 走恢复码兜底：正确恢复码签发 mfa token，错误恢复码被拒。"""
        import hashlib

        user = await _create_user(auth_db, username="stepup_recovery")
        await _enable_totp_for_user(auth_db, user.id)
        auth_db.add(
            RecoveryCode(
                user_id=user.id,
                code_hash=hashlib.sha256(b"rc-abc123").hexdigest(),
                used=False,
            )
        )
        await auth_db.flush()
        token = create_access_token(
            user_id=user.id, account_level="normal", role="member"
        )

        ok = await auth_front_client.post(
            "/api/v1/auth/2fa/step-up",
            headers=_auth(token),
            json={"recovery_code": "rc-abc123"},
        )
        assert ok.status_code == 200
        assert ok.json()["code"] == 0
        payload = decode_access_token(ok.json()["data"]["access_token"])
        assert payload["mfa"] is True

        # 恢复码已原子消费，重复用 → 失败
        again = await auth_front_client.post(
            "/api/v1/auth/2fa/step-up",
            headers=_auth(token),
            json={"recovery_code": "rc-abc123"},
        )
        assert again.status_code == 400
        assert again.json()["code"] != 0


class TestDeleteNot2FAGated:
    """普通用户删除自己的内容不再要求 2FA（danger 2FA 仅保留给管理员代删/删passkey）。"""

    async def should_not_gate_user_delete_with_mfa(
        self, fused_db_session: AsyncSession, fused_front_client: Any
    ):
        """有有效 token 即可删除：删除不由 2FA 门禁拦截（不再返回 401 code=4）。

        注意：RBAC 迁移后，统一内容删除路由先在 check_owner 判定（未拥有
        content.owner_delete 且非属主时，对不存在的内容也统一返回 403 以免泄露资源
        存在性），故此处断言 403 而非旧的 404——核心意图仍是非 2FA 门禁。

        拆库后该路由同时要 auth 身份与业务库 role_permissions/content_items，故用 fused
        （auth+业务同 schema）统一三处会话，令鉴权、权限判定、内容查询都可见。
        """
        user = await _create_user(fused_db_session, username="delete_nogate")
        token = create_access_token(
            user_id=user.id, account_level="normal", role="member"
        )
        resp = await fused_front_client.delete(
            "/api/v1/content/items/00000000-0000-7000-8000-000000000099",
            headers=_auth(token),
        )
        # 能走到权限判定（而非被 2FA 门禁拦住）→ 不再是 401 code=4
        assert resp.status_code == 403


class TestRecoveryCodeHashing:
    """恢复码存储升级：新码用带 pepper 的 HMAC（非裸 SHA-256），校验兼容存量裸哈希。"""

    async def should_store_new_codes_with_hmac(self, db: AsyncSession):
        from app.modules.auth.security import hash_recovery_code

        user = await _create_user(db, username="hash_rc_user")
        # 完整 setup 流程：begin 创建未启用 TOTP → complete 启用并生成恢复码（HMAC 落库）
        begin = await _svc().setup_2fa_begin(db, user.id)
        secret = begin["secret"]
        totp_code = _generate_totp_code(secret)
        await _svc().setup_2fa_complete(db, user.id, code=totp_code)
        rows = (
            (
                await db.execute(
                    select(RecoveryCode).where(RecoveryCode.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows
        # 都是 64 hex 的哈希结果；存的是带 pepper 的 HMAC（与裸 SHA-256 内容不同）
        assert all(len(r.code_hash) == 64 for r in rows)
        assert hash_recovery_code("anything") != hashlib.sha256(b"anything").hexdigest()

    async def should_verify_legacy_and_new_hash(self, db: AsyncSession):
        """consume_recovery_code 对旧裸哈希种子也能消费（向后兼容）。"""
        user = await _create_user(db, username="legacy_rc_user")
        await _enable_totp_for_user(db, user.id)
        db.add(
            RecoveryCode(
                user_id=user.id,
                code_hash=hashlib.sha256(b"legacy-seed").hexdigest(),
                used=False,
            )
        )
        await db.flush()
        await _svc().verify_second_factor(db, user.id, recovery_code="legacy-seed")
        row = (
            (
                await db.execute(
                    select(RecoveryCode).where(
                        RecoveryCode.user_id == user.id,
                        RecoveryCode.code_hash
                        == hashlib.sha256(b"legacy-seed").hexdigest(),
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None and row.used is True


class TestRecoveryCodeRateLock:
    """恢复码暴力尝试在达到上限后锁定（不再放行）。"""

    async def should_lock_after_too_many_failures(self, db: AsyncSession):
        user = await _create_user(db, username="rc_lock_user")
        await _enable_totp_for_user(db, user.id)
        db.add(
            RecoveryCode(
                user_id=user.id,
                code_hash=hashlib.sha256(b"real-code").hexdigest(),
                used=False,
            )
        )
        await db.flush()

        threshold = 3
        for _ in range(threshold):
            with pytest.raises(BizError) as exc:
                await _svc().verify_second_factor(
                    db, user.id, recovery_code="wrong-code"
                )
            assert exc.value.errcode == AuthErr.RECOVERY_CODE_INVALID

        # 已达上限：即使提供正确恢复码也被拒（失败计数锁命中了真实码行）
        for _ in range(2):
            with pytest.raises(BizError) as exc:
                await _svc().verify_second_factor(
                    db, user.id, recovery_code="real-code"
                )
            assert exc.value.errcode == AuthErr.RECOVERY_CODE_INVALID

    async def should_succeed_after_reset_on_success(self, db: AsyncSession):
        user = await _create_user(db, username="rc_reset_user")
        await _enable_totp_for_user(db, user.id)
        db.add(
            RecoveryCode(
                user_id=user.id,
                code_hash=hashlib.sha256(b"code-ok").hexdigest(),
                used=False,
            )
        )
        await db.flush()
        # 失败一次后成功消费：清零计数不锁定
        with pytest.raises(BizError) as exc:
            await _svc().verify_second_factor(db, user.id, recovery_code="bad")
        assert exc.value.errcode == AuthErr.RECOVERY_CODE_INVALID
        await _svc().verify_second_factor(db, user.id, recovery_code="code-ok")
        row = (
            (
                await db.execute(
                    select(RecoveryCode).where(
                        RecoveryCode.user_id == user.id,
                        RecoveryCode.code_hash
                        == hashlib.sha256(b"code-ok").hexdigest(),
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None and row.used is True
