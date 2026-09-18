import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import CommonErr
from tests.conftest import auth_user_uid


async def _setup_user(
    auth_db: AsyncSession, username="tester"
) -> tuple[uuid.UUID, str]:
    """在 auth realm(business 无 users)建 normal/member 用户，返回 (id, access token)。"""
    au = await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@x.com",
        nickname=username,
        account_level="normal",
        role="member",
    )
    return au.id, au.token


class TestStarHopeRoutes:
    async def test_pull_requires_auth(self, client):
        resp = await client.get("/api/v1/starhope/questions")
        assert resp.status_code == 403

    async def test_push_and_pull_roundtrip(
        self, client, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        _, token = await _setup_user(auth_db)
        upserts = [
            {
                "id": "q1",
                "type": "single",
                "content": "1+1=?",
                "options": ["1", "2"],
                "answer": "2",
                "analysis": None,
                "tags": ["数学"],
                "folder_id": None,
                "difficulty": 1,
                "updated_at": "2026-08-15T00:00:00+00:00",
            }
        ]
        resp = await client.post(
            "/api/v1/starhope/questions/sync",
            json={"upserts": upserts, "deletes": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["synced"] == 1

        resp = await client.get(
            "/api/v1/starhope/questions",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"][0]["id"] == "q1"
        assert data["items"][0]["answer"] == "2"

    async def test_invalid_entity_returns_422(
        self, client, db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
    ):
        _, token = await _setup_user(auth_db)
        resp = await client.get(
            "/api/v1/starhope/nope",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] != CommonErr.OK
