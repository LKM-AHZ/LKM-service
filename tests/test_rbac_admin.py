"""后台 admin 模块 RBAC 细粒度权限点叠加测试（Task 7.1）。

后台鉴权是独立 cookie 会话（require_admin，仅判断 account_level=admin），本身不细分。
本测试证明叠加后：admin+org_member 只拿到被授予的 admin 域权限点，admin_users_manage /
admin_content_review 仅 super_admin 持有；未授权即拒绝（403）。

对照收紧点：迁移前任何 account_level=admin 都能进 /admin/users 等；迁移后 org_member
对 users_manage / content_review 是 403。以下是红绿 proof。
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.auth.models import Profile, User
from tests.conftest import DB, Client

# 不存在的后台内容条目 id（uuid7 形态），用于越过 2FA 后落到 service 未命中路径。
_MISSING_ITEM_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


@pytest.fixture
async def db(fused_db_session: AsyncSession) -> AsyncSession:
    """admin+RBAC 用例需 auth(users/profile)+biz(role_permissions) 单 schema（融合装配）。"""
    return fused_db_session


@pytest.fixture(autouse=True)
async def _seam_for_admin(auth_seam_fused) -> None:
    """admin 端点解析 current admin user 走 auth seam → fused 里的 auth 表。"""




async def _mk_user(
    db: DB, uname: str, level: str = "admin", role: str = "org_member"
) -> User:
    """建 admin 用户 + Profile（role 决定复合角色）。hashed_password 放占位值（users NOT NULL）。"""
    user = User(
        username=uname,
        account_level=level,
        hashed_password="rbac-admin-test-placeholder-not-real",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role=role, nickname=uname))
    await db.flush()
    return user


async def _seed_perm(db: DB, role: str, perm: str) -> None:
    """幂等写入 role_permissions，避免撞唯一约束。"""
    exists = await db.scalar(
        select(RolePermission.id).where(
            RolePermission.role_name == role,
            RolePermission.permission == perm,
        )
    )
    if exists is None:
        db.add(RolePermission(role_name=role, permission=perm))
        await db.flush()


def _set_admin_cookie(client: Client, user: User, mfa_verified: bool = False) -> None:
    """把后台 access cookie 写进 httpx jar（可选 2FA 信任）以越过 require_admin 门槛。"""
    tok = create_admin_access_token(user, mfa_verified=mfa_verified)
    client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)


class TestAdminUsersManage:
    async def test_org_member_without_grant_forbidden(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # org_member 默认（DEFAULT_GRANTS）不授 admin_users_manage → 403
        await _seed_perm(db, "admin:org_member", "admin.dashboard")
        u = await _mk_user(db, "org_u", role="org_member")
        _set_admin_cookie(client, u)
        r = await client.get("/api/v1/admin/users")
        assert r.status_code == 403

    async def test_super_admin_with_grant_allowed(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # super_admin 授 admin.users_manage → 200
        await _seed_perm(db, "admin:super_admin", "admin.users_manage")
        u = await _mk_user(db, "sadmin_u", role="super_admin")
        _set_admin_cookie(client, u)
        r = await client.get("/api/v1/admin/users")
        assert r.status_code == 200


class TestAdminReportsView:
    async def test_org_member_with_grant_allowed(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # org_member 默认授 admin_reports_view（DEFAULT_GRANTS）→ 200
        await _seed_perm(db, "admin:org_member", "admin.reports_view")
        u = await _mk_user(db, "org_r", role="org_member")
        _set_admin_cookie(client, u)
        r = await client.get("/api/v1/admin/reports")
        assert r.status_code == 200

    async def test_org_member_dashboard_allowed(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # org_member 默认授 admin_dashboard → /auth/me 与 /stats 可访问
        await _seed_perm(db, "admin:org_member", "admin.dashboard")
        u = await _mk_user(db, "org_d", role="org_member")
        _set_admin_cookie(client, u)
        assert (await client.get("/api/v1/admin/stats")).status_code == 200
        assert (await client.get("/api/v1/admin/auth/me")).status_code == 200


class TestAdminContentReview:
    async def test_org_member_without_grant_forbidden(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # org_member 不授 admin_content_review → 越过 2FA 后仍 403（RBAC 收紧）
        await _seed_perm(db, "admin:org_member", "admin.dashboard")
        u = await _mk_user(db, "org_c", role="org_member")
        _set_admin_cookie(client, u, mfa_verified=True)
        # 真实路由为 /admin/content/item/{item_id}（moderation 收编 admin 后统一）
        r = await client.delete(f"/api/v1/admin/content/item/{_MISSING_ITEM_ID}")
        assert r.status_code == 403

    async def test_super_admin_with_grant_allowed(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # super_admin 授 admin_content_review → 越过 2FA 后到达 service（帖不存在→404）
        await _seed_perm(db, "admin:super_admin", "admin.content_review")
        u = await _mk_user(db, "sadmin_c", role="super_admin")
        _set_admin_cookie(client, u, mfa_verified=True)
        # super_admin 缺 admin.content_review → 越过 2FA 后到达 service（帖不存在→404）
        r = await client.delete(f"/api/v1/admin/content/item/{_MISSING_ITEM_ID}")
        assert r.status_code == 404


class TestAdminModerationManage:
    """审校规则写端点：叠加 require_admin_2fa + admin.moderation_manage 权限点。"""

    async def test_org_member_without_grant_forbidden(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # org_member 不授 admin.moderation_manage → 越过 2FA 后仍 403（RBAC 收紧）
        await _seed_perm(db, "admin:org_member", "admin.dashboard")
        u = await _mk_user(db, "org_mod", role="org_member")
        _set_admin_cookie(client, u, mfa_verified=True)
        r = await client.post(
            "/api/v1/admin/moderation/rules", json={"pattern": "风控", "weight": 0.5}
        )
        assert r.status_code == 403

    async def test_org_member_list_rules_forbidden(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # 规则列表暴露内部正则权重，同样收紧到该权限点
        await _seed_perm(db, "admin:org_member", "admin.dashboard")
        u = await _mk_user(db, "org_mod_l", role="org_member")
        _set_admin_cookie(client, u)
        r = await client.get("/api/v1/admin/moderation/rules")
        assert r.status_code == 403

    async def test_super_admin_with_grant_allowed(
        self,
        db: DB,
        client: Client,
    ) -> None:
        # super_admin 授 admin.moderation_manage → 越过 2FA + RBAC 后创建成功
        await _seed_perm(db, "admin:super_admin", "admin.moderation_manage")
        u = await _mk_user(db, "sadmin_mod", role="super_admin")
        _set_admin_cookie(client, u, mfa_verified=True)
        r = await client.post(
            "/api/v1/admin/moderation/rules", json={"pattern": "风控", "weight": 0.5}
        )
        assert r.status_code == 200
        assert r.json()["data"]["pattern"] == "风控"


class TestAdminMe:
    async def test_super_admin_me_allowed(
        self,
        db: DB,
        client: Client,
    ) -> None:
        await _seed_perm(db, "admin:super_admin", "admin.dashboard")
        u = await _mk_user(db, "sadmin_me", role="super_admin")
        _set_admin_cookie(client, u)
        r = await client.get("/api/v1/admin/auth/me")
        assert r.status_code == 200
        assert r.json()["data"]["account_level"] == "admin"
        assert r.json()["data"]["role"] == "super_admin"
