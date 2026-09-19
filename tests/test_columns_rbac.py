"""columns 迁移 RBAC（M3.B S5 拆库 dual 真 PG 迁移：申请/审核/发布属主）。

拆库后业务库(Base 无 users)不再有 User/Profile；用户名/身份在 auth realm。本文件任一
走 HTTP 鉴权 / review 的用例均须注入 ``auth_db``+``auth_seam_realm``：
- ``_mk_au(auth_db,...)`` 建 auth realm 用户并返回稳定 ``AuthUser(id/token)``；
- ``_h(au)`` 用 auth_user_uid mint 的 Web token（其 claim 已含该用户 account_level/role）；
- ``_set_admin_mfa_cookie`` 照旧用 create_admin_access_token 走后台 cookie（从 auth realm
  现查 User ORM 造 tok），2FA 信任 + handler 内 columns.* 权限点判定经 seam 落地业务 realm。
RolePermission 权限点仍在业务 realm（Base, 符合生产），由 db 直插。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
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
    # AuthUser.token 已在 auth realm 以该用户 (account_level, role) mint 的 Web Bearer。
    return {"Authorization": f"Bearer {au.token}"}


async def _grant(db: DB, role_name: str, *perms: str) -> None:
    for p in perms:
        db.add(RolePermission(role_name=role_name, permission=p))
    await db.flush()


async def test_member_can_apply(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _grant(db, "normal:member", "columns.application_create")
    u = await _mk_au(auth_db, "cm", account_level="normal", role="member")
    r = await client.post(
        "/api/v1/content/columns/applications",
        headers=_h(u),
        json={"title": "t", "description": "d", "reason": "r"},
    )
    assert r.status_code in (200, 201)


async def test_foreign_cannot_publish_to_other_column(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 注意：other 有 columns.publish（可发文）但【不授】column.owner_publish
    # （owner 权限仅 super_admin 持有）。若授了 column.owner_publish，check_owner
    # 会因拥有该权限点放行 → 200 而非 403，测试失真。
    from app.modules.content.models import Column

    await _grant(db, "normal:columnist", "columns.publish")
    owner = await _mk_au(auth_db, "col_owner", role="columnist")
    other = await _mk_au(auth_db, "col_other", role="columnist")
    col = Column(slug="c1", title="C", description="", owner_id=owner.id)
    db.add(col)
    await db.flush()
    r = await client.post(
        f"/api/v1/content/columns/{col.id}/posts",
        headers=_h(other),
        json={"title": "t", "content": "c", "category": "x"},
    )
    assert r.status_code == 403


async def test_owner_can_publish_to_own_column(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 属主本人（有 columns.publish，无 owner 权限点）走 id_field 属主判定放行。
    from app.modules.content.models import Column

    await _grant(db, "normal:columnist", "columns.publish")
    owner = await _mk_au(auth_db, "cowner", role="columnist")
    col = Column(slug="c2", title="C2", description="", owner_id=owner.id)
    db.add(col)
    await db.flush()
    r = await client.post(
        f"/api/v1/content/columns/{col.id}/posts",
        headers=_h(owner),
        json={"title": "t", "content": "c", "category": "x"},
    )
    assert r.status_code in (200, 201)


async def _mk_super_admin(auth_db: AsyncSession, uname: str) -> AuthUser:
    return await _mk_au(auth_db, uname, account_level="admin", role="super_admin")


def _set_admin_mfa_cookie(client: Client, user: User) -> None:
    # review 端点走 require_admin_2fa（后台 cookie + step-up 2FA 信任），
    # 故用后台 access token（mfa_verified=True）写入 client cookie 越过 2FA 门槛，
    # 才触达 handler 内 columns.application_review 权限点判定。与 boards/projects 审核测试一致。
    tok = create_admin_access_token(user, mfa_verified=True)
    client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)


async def test_super_admin_can_review_application(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    from app.modules.content.models import ColumnApplication

    # 申请人普通，super_admin 审核（2FA 门槛 + columns.application_review 权限点）。
    applicant = await _mk_au(auth_db, "app_owner", account_level="normal", role="member")
    await _grant(db, "admin:super_admin", "columns.application_review")
    sa = await _mk_super_admin(auth_db, "cap_member")
    app = ColumnApplication(
        user_id=applicant.id, title="t", description="d", reason="r"
    )
    db.add(app)
    await db.flush()
    _set_admin_mfa_cookie(
        client,
        (
            await auth_db.execute(select(User).where(User.id == sa.id))
        ).scalar_one(),
    )
    r = await client.post(
        f"/api/v1/content/columns/applications/{app.id}/review",
        json={"status": "approved"},
    )
    assert r.status_code in (200, 201)


async def test_member_without_apply_perm_is_403(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 旧代码 apply_column 无权限校验，member 提交任意通过 200；迁移后需
    # columns.application_create。本用例故意【不授】该权限点，锁定迁移新增的强制力：
    # 无权限 member 申请必须 403，否则说明权限点未生效。
    u = await _mk_au(auth_db, "cm_noperm", account_level="normal", role="member")
    r = await client.post(
        "/api/v1/content/columns/applications",
        headers=_h(u),
        json={"title": "t", "description": "d", "reason": "r"},
    )
    # RequirePermission(columns.application_create) → 403（而非 200/201）
    assert r.status_code == 403


async def test_org_member_cannot_review_application(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 旧 review 仅 RequireLevel("admin")，org_member(level=admin) 会被放行 200；
    # 迁移后叠加 columns.application_review，org_member 无此权限 → 403。
    # 该用例验证 super_admin 与 org_member 的真实能力差异（此回归由迁移引入）。
    from app.modules.content.models import ColumnApplication

    applicant = await _mk_au(auth_db, "org_rev_app", role="member")
    app = ColumnApplication(
        user_id=applicant.id, title="t", description="d", reason="r"
    )
    db.add(app)
    await db.flush()
    # org_member 持有 columns.application_create（可申请）但【不授】application_review。
    await _grant(db, "admin:org_member", "columns.application_create")
    u = await _mk_au(auth_db, "org_rev", account_level="admin", role="org_member")
    # 越过 2FA 门槛后仍在 handler 内被判 FORBIDDEN → 403
    _set_admin_mfa_cookie(
        client,
        (await auth_db.execute(select(User).where(User.id == u.id))).scalar_one(),
    )
    r = await client.post(
        f"/api/v1/content/columns/applications/{app.id}/review",
        json={"status": "approved"},
    )
    assert r.status_code == 403


async def test_super_admin_can_publish_to_other_column(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # super_admin 持有 columns.publish + column.owner_publish：可代发任意专栏。
    from app.modules.content.models import Column

    await _grant(
        db,
        "normal:columnist",
        "columns.publish",
    )
    owner = await _mk_au(auth_db, "fowner", role="columnist")
    col = Column(slug="c3", title="C3", description="", owner_id=owner.id)
    db.add(col)
    await db.flush()
    await _grant(
        db,
        "admin:super_admin",
        "columns.publish",
        "column.owner_publish",
    )
    sa = await _mk_au(auth_db, "fsa", account_level="admin", role="super_admin")
    r = await client.post(
        f"/api/v1/content/columns/{col.id}/posts",
        headers=_h(sa),
        json={"title": "t", "content": "c", "category": "x"},
    )
    assert r.status_code in (200, 201)
