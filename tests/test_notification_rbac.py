"""notification 域 HTTP/RBAC 级验证（M6.8）。

权限点落业务库 ``role_permissions``（生产口径），鉴权走 ``auth_db``+``auth_seam_realm``。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import CommonErr
from app.modules.admin.models import RolePermission
from app.modules.notification.models import Notification
from app.modules.notification.service import NotificationType, create_notification
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


async def _seed_normal(db: DB) -> None:
    await _seed_perm(db, "normal:member", "notification.read")
    await _seed_perm(db, "normal:member", "notification.manage")


async def test_anonymous_rejected(client: Client, auth_seam_realm: None) -> None:
    r = await client.get("/api/v1/notification/me")

    assert r.status_code == 403
    assert r.json()["code"] == CommonErr.FORBIDDEN


async def test_local_without_permission_rejected(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_normal(db)
    local = await _mk_au(auth_db, "nlocal", account_level="local")

    r = await client.get("/api/v1/notification/me", headers=_h(local))

    assert r.status_code == 403


async def test_list_and_unread_count(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_normal(db)
    user = await _mk_au(auth_db, "nlist")
    other = await _mk_au(auth_db, "nlist_other")
    await create_notification(
        db, user_id=user.id, type=NotificationType.CONTENT_LIKED, payload={"title": "t"}
    )
    await create_notification(db, user_id=other.id, type=NotificationType.CONTENT_LIKED)

    listed = await client.get("/api/v1/notification/me", headers=_h(user))
    unread = await client.get("/api/v1/notification/me/unread-count", headers=_h(user))

    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1, "只看到自己的通知"
    assert listed.json()["data"]["items"][0]["payload"]["title"] == "t"
    assert unread.json()["data"]["unread"] == 1


async def test_mark_read_all(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_normal(db)
    user = await _mk_au(auth_db, "nread")
    await create_notification(db, user_id=user.id, type=NotificationType.CONTENT_LIKED)

    r = await client.post(
        "/api/v1/notification/me/read", headers=_h(user), json={"all": True}
    )
    unread = await client.get("/api/v1/notification/me/unread-count", headers=_h(user))

    assert r.status_code == 200
    assert r.json()["data"]["updated"] == 1
    assert unread.json()["data"]["unread"] == 0


async def test_preferences_roundtrip(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_normal(db)
    user = await _mk_au(auth_db, "npref")

    before = await client.get("/api/v1/notification/me/preferences", headers=_h(user))
    updated = await client.put(
        "/api/v1/notification/me/preferences",
        headers=_h(user),
        json={"items": [{"type": NotificationType.CONTENT_LIKED, "enabled": False}]},
    )
    after = await client.get("/api/v1/notification/me/preferences", headers=_h(user))

    assert before.status_code == 200
    assert all(i["enabled"] for i in before.json()["data"]["items"])
    assert {
        i["type"]: i["enabled"] for i in updated.json()["data"]["items"]
    }[NotificationType.CONTENT_LIKED] is False
    assert {
        i["type"]: i["enabled"] for i in after.json()["data"]["items"]
    }[NotificationType.CONTENT_LIKED] is False


async def test_unknown_preference_type_rejected(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_normal(db)
    user = await _mk_au(auth_db, "npref_bad")

    r = await client.put(
        "/api/v1/notification/me/preferences",
        headers=_h(user),
        json={"items": [{"type": "bogus", "enabled": False}]},
    )

    assert r.status_code == 422


async def test_token_register_and_delete(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    await _seed_normal(db)
    user = await _mk_au(auth_db, "ntok")

    created = await client.post(
        "/api/v1/notification/me/tokens",
        headers=_h(user),
        json={"token": "device-abc", "platform": "android"},
    )
    again = await client.post(
        "/api/v1/notification/me/tokens",
        headers=_h(user),
        json={"token": "device-abc", "platform": "android"},
    )
    removed = await client.delete(
        "/api/v1/notification/me/tokens?token=device-abc", headers=_h(user)
    )

    assert created.status_code == 200
    assert created.json()["data"]["id"] == again.json()["data"]["id"], "同 token 幂等"
    assert removed.json()["data"]["updated"] == 1


async def test_manage_permission_required_for_writes(
    db: DB, client: Client, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    # 只有 read 权限（无 manage）→ 列表可读、写操作被拒
    await _seed_perm(db, "normal:member", "notification.read")
    user = await _mk_au(auth_db, "nro")

    listed = await client.get("/api/v1/notification/me", headers=_h(user))
    denied = await client.post(
        "/api/v1/notification/me/read", headers=_h(user), json={"all": True}
    )

    assert listed.status_code == 200
    assert denied.status_code == 403


async def test_notification_row_is_created_for_target_user_only(
    db: DB, auth_db: AsyncSession
) -> None:
    """服务层口径：通知行只挂收件人，不带他人（防越权读的底层保证）。"""
    a = await _mk_au(auth_db, "nrow_a")
    b = await _mk_au(auth_db, "nrow_b")
    await create_notification(db, user_id=a.id, type=NotificationType.CONTENT_LIKED)

    rows = (await db.execute(select(Notification))).scalars().all()
    assert [r.user_id for r in rows] == [a.id]
    assert b.id not in [r.user_id for r in rows]
