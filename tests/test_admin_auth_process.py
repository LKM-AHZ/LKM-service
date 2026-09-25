"""AUTH 独立进程后台 admin 会话写面（S5-A2 Step0 additive）HTTP 测试。

验证主送经 ``auth_app_client``（直接起 ``auth.main.app``，override ``get_auth_session``
→ 本测 ``auth_db`` auth 独立库 schema）打 auth 进程自身的 4 个写面端点：

- login 成功落 cookie（admin_session/admin_refresh）
- 错密 / 非 admin / 未知用户 → 403
- refresh rotate（新 refresh 有效、旧 refresh 可复用检测拒绝）
- logout revoke（登出后旧 refresh 再刷新 → 403）
- 2FA step-up：登录后 TOTP 验证通过升级 mfa access

真双 PG 才有意义：auth schema 独立，user/totp/refresh 模型都落在 AuthBase(18 表)。
只改本测试文件新增，不动单体现成文件。
"""

import base64
import hashlib
import hmac
import struct
import time
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import TOTP, Profile, RefreshToken, User
from auth.security import encrypt_secret, generate_totp_secret, hashpwd


async def _create_admin(
    db: AsyncSession, username: str, password: str = "secret123456"
) -> User:
    """在 auth 独立库建一个管理员 User + super_admin Profile（账号 level=admin）。"""
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd(password),
        account_level="admin",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="super_admin", nickname=username))
    await db.commit()
    await db.refresh(user)
    return user


def _login(
    client: AsyncClient, username: str = "root", password: str = "secret123456"
) -> Any:
    """AUTH 进程版 admin 登录；httpx 会把 Set-Cookie 持久化到 client.cookies。"""
    return client.post(
        "/api/v1/admin/auth/login",
        json={"username": username, "password": password},
    )


def _totp_code_now(secret: str) -> str:
    """生成当前时间步的 TOTP 6 位码（window=1 对齐 security.verify_totp）。"""
    key = base64.b32decode(secret, casefold=True)
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    ) % 1_000_000
    return f"{code:06d}"


async def _enable_totp(db: AsyncSession, username: str) -> str:
    """给管理员建已启用 TOTP，返回明文 secret。"""
    user = (
        (await db.execute(select(User).where(User.username == username)))
        .scalars()
        .first()
    )
    assert user is not None
    secret = generate_totp_secret()
    db.add(TOTP(user_id=user.id, secret=encrypt_secret(secret), enabled=True))
    await db.commit()
    return secret


class TestAuthProcessAdminLogin:
    async def should_login_admin_and_set_cookies(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        await _create_admin(auth_db, "root")
        resp = await _login(auth_app_client, "root")
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert body["code"] == 0
        assert body["data"]["account_level"] == "admin"
        assert auth_app_client.cookies.get("admin_session")
        assert auth_app_client.cookies.get("admin_refresh")

    async def should_reject_wrong_password(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        await _create_admin(auth_db, "root")
        resp = await _login(auth_app_client, "root", "wrong-pass")
        assert resp.status_code == 403

    async def should_reject_non_admin_account(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        """auth 库正常账号（account_level=normal）不得进后台。"""
        from .conftest import auth_user_uid

        await auth_user_uid(auth_db, username="member1", account_level="normal")
        resp = await _login(auth_app_client, "member1")
        assert resp.status_code == 403
        assert resp.json()["message"] == "无后台访问权限"

    async def should_reject_unknown_username(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        resp = await _login(auth_app_client, "ghost")
        assert resp.status_code == 403

    async def should_store_admin_refresh_with_kind(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        """登录生成的 refresh 落在 auth 库且 kind=admin（与前台 web 隔离）。"""
        await _create_admin(auth_db, "kind1")
        login = await _login(auth_app_client, "kind1")
        assert login.status_code == 200
        stored = (
            (await auth_db.execute(select(RefreshToken)))
            .scalars()
            .all()
        )
        admin_rows = [r for r in stored if r.kind == "admin"]
        assert admin_rows, "应在 auth 库写入 kind=admin 的 refresh 行"


class TestAuthProcessAdminRefreshAndLogout:
    async def should_rotate_on_refresh(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        await _create_admin(auth_db, "root")
        login = await _login(auth_app_client, "root")
        assert login.status_code == 200
        resp = await auth_app_client.post("/api/v1/admin/auth/refresh")
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        # 旋转后收到新的 refresh cookie
        assert auth_app_client.cookies.get("admin_refresh")

    async def should_logout_and_clear(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        await _create_admin(auth_db, "root")
        login = await _login(auth_app_client, "root")
        assert login.status_code == 200
        logout = await auth_app_client.post("/api/v1/admin/auth/logout")
        assert logout.status_code == 200
        # 登出后 jar cookie 已清；旧 refresh 已撤销 → 直接再刷新被拒
        again = await auth_app_client.post("/api/v1/admin/auth/refresh")
        assert again.status_code == 403

    async def should_reject_reuse_of_old_refresh_after_rotation(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        """旋转后旧 refresh 已在 auth 库 revoked → 手动塞回旧值刷新应被拒（复用检测）。"""
        await _create_admin(auth_db, "root_reuse2")
        login = await _login(auth_app_client, "root_reuse2")
        assert login.status_code == 200

        old_refresh = auth_app_client.cookies.get("admin_refresh")
        assert old_refresh

        first = await auth_app_client.post("/api/v1/admin/auth/refresh")
        assert first.status_code == 200

        # 清掉旋转后写下的新 cookie，塞回旧值（cookie path 需与实际 COOKIE_PATH=/api/v1 一致）
        for cp in ("/api/v1", "/api/v1/admin"):
            auth_app_client.cookies.delete("admin_refresh", path=cp)
        auth_app_client.cookies.set("admin_refresh", old_refresh, path="/api/v1")
        again = await auth_app_client.post("/api/v1/admin/auth/refresh")
        assert again.status_code == 403


class TestAuthProcessAdmin2FA:
    async def should_stepup_upgrade_access(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        """登录后跑 POST /2fa 用真 TOTP 码过 step-up → 返回 mfa_verified=True + 升级 access cookie。"""
        await _create_admin(auth_db, "adm_mfa")
        login = await _login(auth_app_client, "adm_mfa")
        assert login.status_code == 200

        secret = await _enable_totp(auth_db, "adm_mfa")
        stepup = await auth_app_client.post(
            "/api/v1/admin/auth/2fa", json={"code": _totp_code_now(secret)}
        )
        assert stepup.status_code == 200
        body: dict[str, Any] = stepup.json()
        assert body["code"] == 0
        assert body["data"]["mfa_verified"] is True
        assert body["data"]["account_level"] == "admin"

    async def should_reject_bad_code(
        self, auth_db: AsyncSession, auth_app_client: AsyncClient
    ):
        """错 TOTP 码 2fa step-up → 抛 TOTP_CODE_INVALID（400-ish），不升级信任。"""
        await _create_admin(auth_db, "adm_bad")
        login = await _login(auth_app_client, "adm_bad")
        assert login.status_code == 200
        bad = await auth_app_client.post(
            "/api/v1/admin/auth/2fa", json={"code": "000000"}
        )
        assert bad.status_code != 200
