"""projects 迁移 RBAC（M3.B S5 拆库 dual 真 PG 迁移：申请/审核 权限点接线测试）。

申请端点（submit_app）从旧 RequireLevel("normal") 收紧为 RequirePermission(projects_application_create)，
故普通 member 未授权限时现应 403（先前 200，呈现收紧）；审核端点（review_app）走
require_admin_2fa（危险操作 2FA，读 cookie），故审核用例需以 create_admin_access_token(mfa_verified=True)
写入 client cookie 才能越 2FA 门槛。

拆库后业务库(Base 无 users)不再有 User/Profile：任一走 HTTP 鉴权 / review 的用例须注入
``auth_db``+``auth_seam_realm``。RolePermission + ProjectApplication(applicant_id 裸 int)
仍落业务 realm(Base, 符合生产)由 db 直插。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.projects.models import ProjectApplication
from auth.models import User
from tests.conftest import DB, AuthUser, Client, auth_user_uid


async def _mk_au(
    auth_db: AsyncSession,
    uname: str,
    account_level: str = "normal",
    role: str = "member",
) -> AuthUser:
    """在 auth realm 建一线用户并返回其稳定 AuthUser(id/username/account_level/token)。"""
    return await auth_user_uid(
        auth_db,
        username=uname,
        nickname=uname,
        account_level=account_level,
        role=role,
    )


def _h(au: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {au.token}"}


async def _seed_perm(db: DB, role: str, perm: str) -> None:
    # 幂等：判存在再插，避免撞 role_permissions 唯一约束。
    exists = await db.scalar(
        select(RolePermission.id).where(
            RolePermission.role_name == role,
            RolePermission.permission == perm,
        )
    )
    if exists is None:
        db.add(RolePermission(role_name=role, permission=perm))
        await db.flush()


async def _set_admin_mfa_cookie(
    client: Client, auth_db: AsyncSession, au_id: int
) -> None:
    # 审核端点 require_admin_2fa 从 cookie 读后台 access token 且须 mfa_verified 信任。
    # token 从 auth realm 现查 User ORM 造（与 columns/boards/files 审核测试一致）。
    user = (await auth_db.execute(select(User).where(User.id == au_id))).scalar_one()
    tok = create_admin_access_token(user, mfa_verified=True)
    client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)


async def _make_pending_application(
    db: DB, applicant_id: int
) -> ProjectApplication:
    app = ProjectApplication(
        applicant_id=applicant_id,
        title="t",
        summary="s",
        description="d",
        member_claims="[]",
        status="pending",
    )
    db.add(app)
    await db.flush()
    return app


def _app_payload(title: str = "LKM") -> dict[str, object]:
    return {
        "title": title,
        "summary": "s",
        "description": "d",
        "member_claims": [
            {"display_name": "艾尔", "role_in_project": "组长", "user_id": None}
        ],
    }


class TestSubmitAppPermission:
    async def test_member_without_grant_cannot_submit(
        self, db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        # member（normal/member，未授 projects_application_create）提交 → 403（收紧证明）
        u = await _mk_au(auth_db, "m0", account_level="normal", role="member")
        r = await client.post(
            "/api/v1/projects/applications", headers=_h(u), json=_app_payload()
        )
        assert r.status_code == 403

    async def test_member_with_grant_can_submit(
        self, db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        # 授 projects_application_create 的普通用户提交 → 200
        await _seed_perm(db, "normal:member", "projects.application_create")
        u = await _mk_au(auth_db, "m1", account_level="normal", role="member")
        r = await client.post(
            "/api/v1/projects/applications", headers=_h(u), json=_app_payload()
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "pending"

    async def test_no_auth_rejected(self, db: DB, client: Client) -> None:
        # 未登录 → 403
        r = await client.post("/api/v1/projects/applications", json=_app_payload())
        assert r.status_code == 403


class TestReviewAppPermission:
    async def test_org_member_cannot_review(
        self, db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        # org_member 未授 projects_application_review → 越 2FA 门槛后 403
        await _seed_perm(db, "admin:org_member", "projects.application_create")
        reviewer = await _mk_au(auth_db, "org0", account_level="admin", role="org_member")
        applicant = await _mk_au(auth_db, "org_app0", account_level="normal")
        app = await _make_pending_application(db, applicant.id)
        await _set_admin_mfa_cookie(client, auth_db, reviewer.id)
        r = await client.post(
            f"/api/v1/projects/applications/{app.id}/review",
            json={"approve": True},
        )
        assert r.status_code == 403

    async def test_super_admin_can_review(
        self, db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        # super_admin 授 projects_application_review → 审核通过 200
        await _seed_perm(db, "admin:super_admin", "projects.application_review")
        sa = await _mk_au(auth_db, "sadmin", account_level="admin", role="super_admin")
        applicant = await _mk_au(auth_db, "sapp0", account_level="normal")
        app = await _make_pending_application(db, applicant.id)
        await _set_admin_mfa_cookie(client, auth_db, sa.id)
        r = await client.post(
            f"/api/v1/projects/applications/{app.id}/review",
            json={"approve": True},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "approved"

    async def test_review_without_2fa_blocked(
        self, db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        # 无 2FA 信任 cookie → 2FA 门槛拦截（MFA_REQUIRED → 401/403 依据 require_admin_2fa 行为）
        await _seed_perm(db, "admin:super_admin", "projects.application_review")
        await _mk_au(auth_db, "no2fa", account_level="admin", role="super_admin")
        applicant = await _mk_au(auth_db, "n2app", account_level="normal")
        app = await _make_pending_application(db, applicant.id)
        # 注意：不 set 2FA cookie
        r = await client.post(
            f"/api/v1/projects/applications/{app.id}/review",
            json={"approve": True},
        )
        assert r.status_code in (401, 403, 428)
