"""follow 查询端点测试：关注状态、我关注的用户/版块列表。

拆库(M3.B S5 dual 真 PG)：users/profiles 迁 auth realm。feed 表只存用户裸 uuid user_id，
涉及身份是性(follow 目标存在)/展示名(list_following)的读走 auth snapshot 缝——业务 service
不直读 auth.users。故本域测试用户在 auth_db(auth_user_uid) 建、取稳定 id+token，并逐测显式打开
``auth_seam_realm`` 把缝指到本测 auth 真值(seam 关则回落就地 select(User)打已拆走的业务 users)。

覆盖：
- service 层：is_following_user / list_following_users / list_followed_boards
- HTTP：GET /users/me/following、GET /boards/me/following（均需登录）、
        GET /users/{id}/follow/status（匿名恒 False、已登录看实际状态）
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.interaction import service as follow_service
from app.modules.interaction.service import (
    is_following_user,
    list_followed_boards,
    list_following_users,
)
from tests.conftest import AuthUser, auth_user_uid


async def _user(auth_db: AsyncSession, username: str, email: str) -> AuthUser:
    """在 auth realm(business realm 无 users) 建 normal/member 用户返稳定 AuthUser(id+token)。"""
    return await auth_user_uid(
        auth_db,
        username=username,
        email=email,
        nickname=username,
        account_level="normal",
        role="member",
    )


def _hdr(u: AuthUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {u.token}"}


async def _board(db: AsyncSession, slug: str) -> uuid.UUID:
    from app.modules.content.boards.schemas import BoardCreate
    from app.modules.content.boards.service import create_board_ex

    return (await create_board_ex(db, BoardCreate(slug=slug, title=slug), None)).id


class TestFollowService:
    async def test_is_following_user(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _user(auth_db, "alice", "alice@ex.com")
        b = await _user(auth_db, "bob", "bob@ex.com")
        assert await is_following_user(db, a.id, b.id) is False
        await follow_service.follow_user(db, a.id, b.id)
        assert await is_following_user(db, a.id, b.id) is True
        await follow_service.unfollow_user(db, a.id, b.id)
        assert await is_following_user(db, a.id, b.id) is False

    async def test_list_following_users_returns_display_name(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _user(auth_db, "alice", "a@ex.com")
        b = await _user(auth_db, "bob", "b@ex.com")
        await follow_service.follow_user(db, a.id, b.id)
        rows = await list_following_users(db, a.id)
        assert [(uid, name) for uid, name, _ in rows] == [(b.id, "bob")]

    async def test_list_followed_boards(
        self, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ) -> None:
        a = await _user(auth_db, "alice", "aa@ex.com")
        bid = await _board(db, "tech")
        await follow_service.follow_board(db, a.id, bid)
        rows = await list_followed_boards(db, a.id)
        assert rows == [(bid, "tech")]


class TestFollowHttp:
    async def test_status_anonymous_is_false(
        self, db: AsyncSession, auth_db: AsyncSession,
        auth_seam_realm: None, client: AsyncClient,
    ) -> None:
        target = await _user(auth_db, "target", "t@ex.com")
        resp = await client.get(f"/api/v1/users/{target.id}/follow/status")
        assert resp.status_code == 200
        assert resp.json()["data"]["is_following"] is False

    async def test_status_authenticated_reflects_follow(
        self, db: AsyncSession, auth_db: AsyncSession,
        auth_seam_realm: None, client: AsyncClient,
    ) -> None:
        me = await _user(auth_db, "me", "me@ex.com")
        target = await _user(auth_db, "target", "t2@ex.com")
        await follow_service.follow_user(db, me.id, target.id)
        resp = await client.get(
            f"/api/v1/users/{target.id}/follow/status", headers=_hdr(me)
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["is_following"] is True

    async def test_following_users_list(
        self, db: AsyncSession, auth_db: AsyncSession,
        auth_seam_realm: None, client: AsyncClient,
    ) -> None:
        me = await _user(auth_db, "me", "m1@ex.com")
        b1 = await _user(auth_db, "b1", "b1@ex.com")
        b2 = await _user(auth_db, "b2", "b2@ex.com")
        await follow_service.follow_user(db, me.id, b1.id)
        await follow_service.follow_user(db, me.id, b2.id)
        resp = await client.get("/api/v1/users/me/following", headers=_hdr(me))
        assert resp.status_code == 200
        ids = {it["user_id"] for it in resp.json()["data"]["items"]}
        assert ids == {str(b1.id), str(b2.id)}

    async def test_following_boards_list(
        self, db: AsyncSession, auth_db: AsyncSession,
        auth_seam_realm: None, client: AsyncClient,
    ) -> None:
        me = await _user(auth_db, "me", "m2@ex.com")
        bid = await _board(db, "dev")
        await follow_service.follow_board(db, me.id, bid)
        resp = await client.get("/api/v1/content/boards/me/following", headers=_hdr(me))
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert items[0]["board_id"] == str(bid) and items[0]["title"] == "dev"

    async def test_following_requires_login(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/api/v1/users/me/following")
        assert resp.status_code == 403
