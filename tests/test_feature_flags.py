"""资源级功能开关：板块/专栏的 features JSONB 公开读与属主/管理员合并写。

覆盖：默认空表、公开读、owner 可改、admin 可代改、非 owner 403、非法 key 422、
超过 50 键 422、资源不存在 404、PATCH 为合并语义。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import RolePermission
from app.modules.columns.models import Column
from app.modules.content.models import Board
from tests.conftest import AuthUser, Client, auth_user_uid

_MISSING_ID = uuid.UUID("00000000-0000-7000-8000-000000000999")


async def _mk_au(
    auth_db: AsyncSession,
    uname: str,
    account_level: str = "normal",
    role: str = "member",
) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=uname,
        nickname=uname,
        account_level=account_level,
        role=role,
    )


def _h(au: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {au.token}"}


async def _grant(db: AsyncSession, role_name: str, *perms: str) -> None:
    for p in perms:
        db.add(RolePermission(role_name=role_name, permission=p))
    await db.flush()


async def _board(db: AsyncSession, slug: str, owner_id: uuid.UUID | None) -> Board:
    board = Board(slug=slug, title=slug, description="d", owner_id=owner_id)
    db.add(board)
    await db.flush()
    return board


async def _column(db: AsyncSession, owner_id: uuid.UUID, title: str) -> Column:
    column = Column(owner_id=owner_id, title=title, description="d")
    db.add(column)
    await db.flush()
    return column


async def test_board_features_default_empty(
    db: AsyncSession, client: Client
) -> None:
    board = await _board(db, "feat-default", None)
    r = await client.get(f"/api/v1/content/boards/{board.id}/features")
    assert r.status_code == 200
    assert r.json()["data"] == {}


async def test_column_features_default_empty(
    db: AsyncSession, client: Client
) -> None:
    column = await _column(db, uuid.uuid4(), "默认空")
    r = await client.get(f"/api/v1/content/columns/{column.id}/features")
    assert r.status_code == 200
    assert r.json()["data"] == {}


async def test_board_features_missing_404(client: Client) -> None:
    r = await client.get(f"/api/v1/content/boards/{_MISSING_ID}/features")
    assert r.status_code == 404


async def test_column_features_missing_404(client: Client) -> None:
    r = await client.get(f"/api/v1/content/columns/{_MISSING_ID}/features")
    assert r.status_code == 404


async def test_board_owner_patches_and_merges(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    owner = await _mk_au(auth_db, "bf_owner")
    board = await _board(db, "feat-owner", owner.id)

    r = await client.patch(
        f"/api/v1/content/boards/{board.id}/features",
        headers=_h(owner),
        json={"comments": True},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"comments": True}

    # 合并语义：第二次只覆盖给出的键
    r = await client.patch(
        f"/api/v1/content/boards/{board.id}/features",
        headers=_h(owner),
        json={"comments": False, "polls": True},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"comments": False, "polls": True}


async def test_board_admin_can_patch(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    await _grant(db, "admin:super_admin", "board.owner_manage")
    owner = await _mk_au(auth_db, "bf_owner2")
    admin = await _mk_au(auth_db, "bf_admin", account_level="admin", role="super_admin")
    board = await _board(db, "feat-admin", owner.id)

    r = await client.patch(
        f"/api/v1/content/boards/{board.id}/features",
        headers=_h(admin),
        json={"polls": True},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"polls": True}


async def test_board_non_owner_forbidden(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    owner = await _mk_au(auth_db, "bf_owner3")
    loser = await _mk_au(auth_db, "bf_loser")
    board = await _board(db, "feat-forbid", owner.id)

    r = await client.patch(
        f"/api/v1/content/boards/{board.id}/features",
        headers=_h(loser),
        json={"polls": True},
    )
    assert r.status_code == 403


async def test_board_invalid_key_is_422(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    owner = await _mk_au(auth_db, "bf_owner4")
    board = await _board(db, "feat-badkey", owner.id)

    r = await client.patch(
        f"/api/v1/content/boards/{board.id}/features",
        headers=_h(owner),
        json={"Bad Key": True},
    )
    assert r.status_code == 422


async def test_board_too_many_keys_is_422(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    owner = await _mk_au(auth_db, "bf_owner5")
    board = await _board(db, "feat-toomany", owner.id)

    r = await client.patch(
        f"/api/v1/content/boards/{board.id}/features",
        headers=_h(owner),
        json={f"k{i}": True for i in range(51)},
    )
    assert r.status_code == 422


async def test_column_owner_patches(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    owner = await _mk_au(auth_db, "cf_owner")
    column = await _column(db, owner.id, "开关键")

    r = await client.patch(
        f"/api/v1/content/columns/{column.id}/features",
        headers=_h(owner),
        json={"tips": True},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"tips": True}

    r = await client.get(f"/api/v1/content/columns/{column.id}/features")
    assert r.json()["data"] == {"tips": True}


async def test_column_admin_can_patch(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    await _grant(db, "admin:super_admin", "column.owner_publish")
    owner = await _mk_au(auth_db, "cf_owner2")
    admin = await _mk_au(auth_db, "cf_admin", account_level="admin", role="super_admin")
    column = await _column(db, owner.id, "管理员代改")

    r = await client.patch(
        f"/api/v1/content/columns/{column.id}/features",
        headers=_h(admin),
        json={"tips": True},
    )
    assert r.status_code == 200


async def test_column_non_owner_forbidden(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    owner = await _mk_au(auth_db, "cf_owner3")
    loser = await _mk_au(auth_db, "cf_loser")
    column = await _column(db, owner.id, "非属主")

    r = await client.patch(
        f"/api/v1/content/columns/{column.id}/features",
        headers=_h(loser),
        json={"tips": True},
    )
    assert r.status_code == 403


async def test_column_invalid_key_is_422(
    db: AsyncSession,
    client: Client,
    auth_db: AsyncSession,
    auth_seam_realm: None,
) -> None:
    owner = await _mk_au(auth_db, "cf_owner4")
    column = await _column(db, owner.id, "非法键")

    r = await client.patch(
        f"/api/v1/content/columns/{column.id}/features",
        headers=_h(owner),
        json={"UPPER": True},
    )
    assert r.status_code == 422
