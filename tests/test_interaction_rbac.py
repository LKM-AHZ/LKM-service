"""interaction 域 HTTP/RBAC 级验证（M6.6）。

沿用 content RBAC 测试口径：业务库无 users，凡走 HTTP 鉴权的用例注入
``auth_db``+``auth_seam_realm``，权限点落业务库 ``role_permissions``（生产口径）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import CommonErr
from app.modules.admin.models import RolePermission
from app.modules.content.models import Board, ContentItem, ContentStatus, ContentType
from tests.conftest import DB, AuthUser, Client, auth_user_uid


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


async def _mk_item(db: DB, author_id: int | None = None) -> int:
    board = Board(slug="i1", title="B", description="", status="active")
    db.add(board)
    await db.flush()
    item = ContentItem(
        content_type=ContentType.DISCUSSION,
        board_id=board.id,
        author_id=author_id,
        title="帖子",
        content="正文",
        status=ContentStatus.PUBLISHED,
    )
    db.add(item)
    await db.flush()
    return int(item.id)


async def test_anonymous_favorite_rejected(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "interaction.favorite")
    item_id = await _mk_item(db)

    r = await client.post(f"/api/v1/interaction/favorites/{item_id}")

    # 缺 token 走 _parse_bearer → CommonErr.FORBIDDEN（403），与全站鉴权口径一致
    assert r.status_code == 403
    assert r.json()["code"] == CommonErr.FORBIDDEN


async def test_local_cannot_favorite(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "interaction.favorite")
    item_id = await _mk_item(db)
    local = await _mk_au(auth_db, "local_u", account_level="local")

    r = await client.post(f"/api/v1/interaction/favorites/{item_id}", headers=_h(local))

    assert r.status_code == 403


async def test_normal_can_favorite_and_unfavorite(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "interaction.favorite")
    item_id = await _mk_item(db)
    user = await _mk_au(auth_db, "nomo")

    added = await client.post(
        f"/api/v1/interaction/favorites/{item_id}", headers=_h(user)
    )
    again = await client.post(
        f"/api/v1/interaction/favorites/{item_id}", headers=_h(user)
    )
    removed = await client.delete(
        f"/api/v1/interaction/favorites/{item_id}", headers=_h(user)
    )

    assert added.status_code == 200 and added.json()["code"] == CommonErr.OK
    assert added.json()["data"] == {
        "content_id": item_id,
        "favorited": True,
        "bookmark_count": 1,
    }
    assert again.json()["data"]["bookmark_count"] == 1, "重复收藏不重复计数"
    assert removed.json()["data"]["favorited"] is False
    assert removed.json()["data"]["bookmark_count"] == 0


async def test_missing_content_returns_404(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "interaction.favorite")
    user = await _mk_au(auth_db, "nomo2")

    r = await client.post("/api/v1/interaction/favorites/999999", headers=_h(user))

    assert r.status_code == 404


async def test_my_favorites_requires_login_only(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    user = await _mk_au(auth_db, "nomo3")
    item_id = await _mk_item(db)
    await _seed_perm(db, "normal:member", "interaction.favorite")
    await client.post(f"/api/v1/interaction/favorites/{item_id}", headers=_h(user))

    r = await client.get("/api/v1/interaction/me/favorites", headers=_h(user))

    assert r.status_code == 200
    body = r.json()
    assert body["code"] == CommonErr.OK
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["content_id"] == item_id


async def test_local_cannot_report_view(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "interaction.history")
    item_id = await _mk_item(db)
    local = await _mk_au(auth_db, "local_v", account_level="local")

    r = await client.post(f"/api/v1/interaction/views/{item_id}", headers=_h(local))

    assert r.status_code == 403


async def test_normal_report_view_idempotent(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_perm(db, "normal:member", "interaction.history")
    item_id = await _mk_item(db)
    user = await _mk_au(auth_db, "nomo_v")

    first = await client.post(f"/api/v1/interaction/views/{item_id}", headers=_h(user))
    second = await client.post(f"/api/v1/interaction/views/{item_id}", headers=_h(user))
    history = await client.get("/api/v1/interaction/me/history", headers=_h(user))

    assert first.status_code == second.status_code == 200
    assert history.json()["data"]["total"] == 1, "重复上报不新增历史行"
