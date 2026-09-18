"""后台用户管理端点 /admin/users 与 /admin/stats(/trend) 的专项测试（模块9 测试补盲）。

S5-A2 Step2 版本：users/profiles 真值迁 auth 库后，本文件一律经 **auth realm**(auth_db)
造 user + admin，权限点 RolePermission 落 **biz realm**(db)。monolith seam(``auth_seam_realm``)
把鉴权缝指到 auth_db 真值；admin reader(user 列表/数/趋势) 经 biz 端点注入的 auth 只读会话
(conftest client 覆盖 get_admin_auth_read_session→auth_db) 读到 auth authoritative。
不触发后台 HTTP login：直接以 create_admin_access_token 在 client jar 置后台 cookie（同源）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.deps import COOKIE_NAME, COOKIE_PATH, create_admin_access_token
from app.modules.admin.models import RolePermission
from app.modules.auth.models import User
from app.modules.content.models import Board, ContentItem, ContentType
from app.modules.files.models import LibraryFile
from app.modules.rbac.permissions import Permission
from tests.conftest import auth_user_uid  # type: ignore[attr-defined]


async def _mk_auth_user(
    auth_db: AsyncSession,
    username: str,
    *,
    account_level: str = "normal",
    role: str = "member",
    email: str | None = None,
) -> User:
    """在 auth realm 造 User(+Profile)；返回该 User 的 auth 库 ORM（供 mint admin cookie）。"""
    au = await auth_user_uid(
        auth_db,
        username=username,
        account_level=account_level,
        role=role,
        email=email,
        nickname=username,
        with_token=False,
    )
    return (await auth_db.execute(select(User).where(User.id == au.id))).scalar_one()


async def _mk_admin(
    auth_db: AsyncSession, username: str = "root"
) -> User:  # account_level=admin + super_admin role
    return await _mk_auth_user(
        auth_db, username, account_level="admin", role="super_admin"
    )


def _set_admin_cookie(client: AsyncClient, user: User) -> None:
    client.cookies.set(
        COOKIE_NAME,
        create_admin_access_token(user),
        path=COOKIE_PATH,
    )


async def _grant_super_admin(db: AsyncSession, *perms: Permission) -> None:
    for p in perms:
        exists = await db.scalar(
            select(RolePermission.id).where(
                RolePermission.role_name == "admin:super_admin",
                RolePermission.permission == p.value,
            )
        )
        if exists is None:
            db.add(RolePermission(role_name="admin:super_admin", permission=p.value))
    await db.flush()


class TestAdminUsersList:
    async def should_reject_non_admin(
        self, client: AsyncClient, auth_db: AsyncSession
    ) -> None:
        member = await _mk_auth_user(auth_db, "member1")  # normal → seam reject
        _set_admin_cookie(client, member)
        resp = await client.get("/api/v1/admin/users")
        assert resp.status_code in (401, 403)

    async def should_list_users_preview_without_pii(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        await _mk_auth_user(auth_db, "alice", email="a@priv.io")
        await _grant_super_admin(
            db, Permission.admin_users_manage, Permission.admin_dashboard
        )
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/users")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["total"] >= 2
        alice = next(i for i in body["items"] if i["username"] == "alice")
        assert alice["email"] is None
        assert alice["phone"] is None
        assert alice["is_locked"] is False
        assert alice["account_level"] == "normal"

    async def should_include_pii_when_requested(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        await _mk_auth_user(auth_db, "bob", email="bob@priv.io")
        await _grant_super_admin(db, Permission.admin_users_manage)
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/users", params={"include_pii": "true"})
        assert resp.status_code == 200
        bob = next(i for i in resp.json()["data"]["items"] if i["username"] == "bob")
        assert bob["email"] == "bob@priv.io"

    async def should_filter_users_by_keyword(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        await _mk_auth_user(auth_db, "zhangsan")
        await _mk_auth_user(auth_db, "lisi")
        await _grant_super_admin(db, Permission.admin_users_manage)
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/users", params={"keyword": "zhang"})
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert all("zhang" in i["username"] for i in items)
        assert any(i["username"] == "zhangsan" for i in items)


async def _seed_stats(
    db: AsyncSession, auth_db: AsyncSession, *, n_users: int = 3
) -> User:
    """root(admin) + u0..u{n-1} 在 auth realm；content/file(biz realm) 引用 auth 裸 id。"""
    root = await _mk_admin(auth_db, "root")
    ids: list[uuid.UUID] = []
    for i in range(n_users):
        au = await auth_user_uid(
            auth_db, username=f"u{i}", account_level="local", with_token=False
        )
        ids.append(au.id)
    board = Board(slug="stats", title="统计", description="", is_public=True)
    db.add(board)
    await db.flush()
    db.add(
        ContentItem(
            content_type=ContentType.DISCUSSION,
            author_id=ids[0],
            board_id=board.id,
            title="t",
            excerpt="",
            content="c",
            tags="[]",
        )
    )
    db.add(
        LibraryFile(
            uploader_id=ids[1],
            original_name="f.pdf",
            stored_name="f.pdf",
            mime_type="application/pdf",
            size=10,
            status="pending",
        )
    )
    await db.commit()
    return root


class TestAdminStats:
    async def should_aggregate_counts(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _seed_stats(db, auth_db)  # root+u0..u2 共4用户(auth) 1帖1文件(biz)
        await _grant_super_admin(db, Permission.admin_dashboard)
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/stats")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["user_count"] >= 4  # auth authoritative 总数（含 root+u0..u2）
        assert data["post_count"] >= 1
        assert data["file_count"] >= 1
        assert data["file_pending_count"] >= 1

    async def should_be_tolerant_of_missing_tables(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        await _grant_super_admin(db, Permission.admin_dashboard)
        _set_admin_cookie(client, root)
        resp = await client.get("/api/v1/admin/stats")
        assert resp.status_code == 200
        assert resp.json()["code"] == 0


class TestAdminTrend:
    async def should_reject_non_admin(
        self, client: AsyncClient, auth_db: AsyncSession
    ) -> None:
        member = await _mk_auth_user(auth_db, "member1")
        _set_admin_cookie(client, member)
        resp = await client.get("/api/v1/admin/stats/trend")
        assert resp.status_code in (401, 403)

    async def should_return_daily_deltas(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _seed_stats(db, auth_db)  # root+u0..u2 共4用户(auth)，1帖(biz)
        await _grant_super_admin(db, Permission.admin_dashboard)
        _set_admin_cookie(client, root)

        resp = await client.get("/api/v1/admin/stats/trend", params={"days": 7})
        assert resp.status_code == 200
        data = resp.json()["data"]
        items = data["items"]
        assert len(items) == 7
        # UTC 基准序列升序、缺日补0；今日(末) user_delta 至少含新增4个同日增量
        today = items[-1]
        assert today["user_delta"] >= 4
        assert today["post_delta"] >= 1
        for i in range(1, len(items)):
            assert items[i]["date"] > items[i - 1]["date"]

    async def should_validate_days_bounds(
        self,
        db: AsyncSession,
        client: AsyncClient,
        auth_db: AsyncSession,
        auth_seam_realm: None,
    ) -> None:
        root = await _mk_admin(auth_db, "root")
        _set_admin_cookie(client, root)
        resp = await client.get("/api/v1/admin/stats/trend", params={"days": 0})
        assert resp.status_code == 422
