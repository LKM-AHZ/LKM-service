"""content RBAC 迁移：发帖权限点、发言准入（认证）、删帖属主校验的 HTTP 级验证。

由 forum 测试迁移（test_boards_forum / test_forum_rbac）承接，URL/权限点/字段对齐
content 端点（/api/v1/content/items）与 content schema（discussion 发帖不带专栏）。

拆库后业务库(Base 无 users)不再有 User/Profile：任一走 HTTP 鉴权 / 认证判定 / 属主
裁决 / 展示读的用例注入 ``auth_db``+``auth_seam_realm``：
- ``_mk_au(auth_db,...)`` 建 auth realm 用户返回稳定 ``AuthUser(id/token)``；
- ``_h(au)`` 用 AuthUser.token 作 Web Bearer（claim 已含该用户 account_level/role）；
- 业务内容项/考试/认证行（author_id/owner_id/user_id）只写 auth realm 的裸 int .id；
- 权限点 RolePermission + Exam/ExamCertificate 仍落业务 realm(Base, 符合生产)。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import CommonErr
from app.modules.admin.models import RolePermission
from app.modules.content.boards.errors import BoardErr
from app.modules.content.models import Board
from app.modules.exam.models import Exam, ExamCertificate
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


async def _mk_board(db: DB, *, owner_id: uuid.UUID | None = None) -> uuid.UUID:
    board = Board(slug="b1", title="B", description="", owner_id=owner_id)
    db.add(board)
    await db.flush()
    return board.id


async def _seed_perm(db: DB, role: str, permission: str) -> None:
    exists = await db.scalar(
        select(RolePermission.id).where(
            RolePermission.role_name == role,
            RolePermission.permission == permission,
        )
    )
    if exists is None:
        db.add(RolePermission(role_name=role, permission=permission))
        await db.flush()


async def _certified_user(auth_db: AsyncSession, cert_db: DB, uname: str) -> AuthUser:
    """建 auth realm 用户 + 在业务 realm 给 TA 一张通识课通过证书(裸 int user_id)。"""
    u = await _mk_au(auth_db, uname)
    exam = Exam(type="exam", title="初级", unlock_level="normal")
    cert_db.add(exam)
    await cert_db.flush()
    cert_db.add(
        ExamCertificate(
            user_id=u.id, exam_id=exam.id, passed=True, cert_no=f"CERT-C-{uname}"
        )
    )
    await cert_db.flush()
    return u


async def test_normal_can_post(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "content.create")
    board = await _mk_board(db)
    user = await _mk_au(auth_db, "nomo", account_level="normal", role="member")
    r = await client.post(
        "/api/v1/content/items",
        headers=_h(user),
        json={"board_id": str(board), "title": "t", "content": "c"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == 0
    assert r.json()["data"]["author_id"] == str(user.id)
    assert r.json()["data"]["content_type"] == "discussion"


async def test_local_cannot_post(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "content.create")
    board = await _mk_board(db)
    user = await _mk_au(auth_db, "local_u", account_level="local", role="member")
    r = await client.post(
        "/api/v1/content/items",
        headers=_h(user),
        json={"board_id": str(board), "title": "t", "content": "c"},
    )
    assert r.status_code == 403


async def test_uncertified_blocked_on_certified_board(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """require_certified 板块 + 未通过通识考试用户发帖 → CERTIFICATION_REQUIRED。"""
    await _seed_perm(db, "normal:member", "content.create")
    owner = await _mk_au(auth_db, "owner", role="member")
    board = Board(
        slug="cert",
        title="C",
        description="",
        owner_id=owner.id,
        require_certified=True,
    )
    db.add(board)
    await db.flush()
    novice = await _mk_au(auth_db, "novice", role="member")

    r = await client.post(
        "/api/v1/content/items",
        headers=_h(novice),
        json={"board_id": str(board.id), "title": "t", "content": "c"},
    )

    assert r.status_code == 403
    assert r.json()["code"] == BoardErr.CERTIFICATION_REQUIRED


async def test_certified_allowed_on_certified_board(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """通过通识考试的用户可在 require_certified 板块发帖。"""
    await _seed_perm(db, "normal:member", "content.create")
    owner = await _mk_au(auth_db, "owner2", role="member")
    board = Board(
        slug="cert2",
        title="C2",
        description="",
        owner_id=owner.id,
        require_certified=True,
    )
    db.add(board)
    await db.flush()
    cert_user = await _certified_user(auth_db, db, "certified")

    r = await client.post(
        "/api/v1/content/items",
        headers=_h(cert_user),
        json={"board_id": str(board.id), "title": "t", "content": "c"},
    )

    assert r.status_code == 200
    assert r.json()["code"] == 0


async def test_foreign_cannot_delete(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "content.create")
    board = await _mk_board(db)
    a = await _mk_au(auth_db, "fred", role="member")
    b = await _mk_au(auth_db, "alice", role="member")
    rp = await client.post(
        "/api/v1/content/items",
        headers=_h(a),
        json={"board_id": str(board), "title": "t", "content": "c"},
    )
    item_id = rp.json()["data"]["id"]
    r = await client.delete(f"/api/v1/content/items/{item_id}", headers=_h(b))
    assert r.status_code == 403
    assert r.json()["code"] == CommonErr.FORBIDDEN


async def test_owner_can_delete(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "content.create")
    board = await _mk_board(db)
    a = await _mk_au(auth_db, "owner_u", role="member")
    rp = await client.post(
        "/api/v1/content/items",
        headers=_h(a),
        json={"board_id": str(board), "title": "t", "content": "c"},
    )
    item_id = rp.json()["data"]["id"]
    r = await client.delete(f"/api/v1/content/items/{item_id}", headers=_h(a))
    assert r.status_code == 200
