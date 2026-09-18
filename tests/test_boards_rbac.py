"""boards 迁移 RBAC（M3.B S5 拆库 dual 真 PG 迁移：申请/审核/属主管理）。

拆库后业务库(Base 无 users)不再有 User/Profile；用户名/身份在 auth realm。本文件任一
走 HTTP 鉴权 / review / 属主裁决的用例均须注入 ``auth_db``+``auth_seam_realm``：
- ``_mk_au(auth_db,...)`` 建 auth realm 用户并返回稳定 ``AuthUser(id/token)``；
- ``_h(au)`` 用 auth_user_uid mint 的 Web token（其 claim 已含该用户 account_level/role）；
- ``_set_admin_mfa_cookie`` 照旧用 create_admin_access_token 走后台 cookie（从 auth realm
  现查 User ORM 造 tok），2FA 信任 + handler 内 boards.* 权限点判定经 seam 落地业务 realm。
RolePermission 权限点仍在业务 realm（Base, 符合生产），由 db 直插。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.auth.models import User
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


async def _set_admin_mfa_cookie(
    client: Client, auth_db: AsyncSession, au_id: uuid.UUID
) -> None:
    # review 端点走 require_admin_2fa（后台 cookie + step-up 2FA 信任）→ 越过 2FA 门槛
    # 才触达 handler 内 boards.review_application 权限点判定。token 从 auth realm 现查
    # User ORM 造（后台 cookie 走独立会话管理；与 columns/files/projects 审核测试一致）。
    user = (await auth_db.execute(select(User).where(User.id == au_id))).scalar_one()
    tok = create_admin_access_token(user, mfa_verified=True)
    client.cookies.set(COOKIE_NAME, tok, path=COOKIE_PATH)


async def test_member_can_apply(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _grant(db, "normal:member", "boards.create_application")
    u = await _mk_au(auth_db, "bm", account_level="normal", role="member")
    r = await client.post(
        "/api/v1/content/boards/applications",
        headers=_h(u),
        json={"title": "t", "description": "d", "reason": "r", "slug": "b1"},
    )
    assert r.status_code in (200, 201)


async def test_org_cannot_review_application(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # org_member 未授予 boards_review_application → 403（先越 2FA 门槛再判权限）。
    from app.modules.content.models import BoardApplication

    await _grant(db, "admin:org_member", "boards.create_application")
    applicant = await _mk_au(auth_db, "org3", account_level="normal", role="member")
    # 先建一个待审申请（直插 DB，applicant_id = auth realm 稳定 uuid）
    app = BoardApplication(
        applicant_id=applicant.id,
        title="t",
        description="d",
        reason="r",
        slug="z1",
        status="pending",
    )
    db.add(app)
    await db.flush()
    org = await _mk_au(auth_db, "org0", account_level="admin", role="org_member")
    await _set_admin_mfa_cookie(client, auth_db, org.id)
    r = await client.post(
        f"/api/v1/content/boards/applications/{app.id}/review",
        json={"approve": True},
    )
    assert r.status_code == 403


async def test_super_admin_can_review_application(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # super_admin 授予 boards_review_application → 审核通过 200。
    from app.modules.content.models import BoardApplication

    await _grant(db, "admin:super_admin", "boards.review_application")
    applicant = await _mk_au(auth_db, "ba2", account_level="normal", role="member")
    sa = await _mk_au(auth_db, "sadmin", account_level="admin", role="super_admin")
    app = BoardApplication(
        applicant_id=applicant.id,
        title="t",
        description="d",
        reason="r",
        slug="z2",
        status="pending",
    )
    db.add(app)
    await db.flush()
    await _set_admin_mfa_cookie(client, auth_db, sa.id)
    r = await client.post(
        f"/api/v1/content/boards/applications/{app.id}/review",
        json={"approve": True},
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "approved"


async def test_owner_can_update_board(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    from app.modules.content.boards.schemas import BoardCreate
    from app.modules.content.boards.service import create_board_ex
    from app.modules.content.models import Board

    owner = await _mk_au(auth_db, "ow1")
    await create_board_ex(db, BoardCreate(slug="ob1", title="原题"), owner.id)
    board_id = await db.scalar(select(Board.id).where(Board.slug == "ob1"))
    r = await client.patch(
        f"/api/v1/content/boards/{board_id}",
        headers=_h(owner),
        json={"title": "新题"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["title"] == "新题"


async def test_non_owner_update_forbidden(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    from app.modules.content.boards.schemas import BoardCreate
    from app.modules.content.boards.service import create_board_ex
    from app.modules.content.models import Board

    owner = await _mk_au(auth_db, "ow2")
    loser = await _mk_au(auth_db, "loser")
    await create_board_ex(db, BoardCreate(slug="ob2", title="别动"), owner.id)
    # non-owner 普通用户（level=normal, role=member，无 board_owner_manage）→ 403
    r = await client.patch(
        f"/api/v1/content/boards/{await db.scalar(select(Board.id).where(Board.slug == 'ob2'))}",
        headers=_h(loser),
        json={"title": "篡改"},
    )
    assert r.status_code == 403
