"""HTTP-level route tests for onboarding + by-username endpoints.

Verifies the routers are actually mounted under /api/v1 and behave end-to-end:
- GET  /api/v1/auth/onboarding
- PUT  /api/v1/auth/onboarding/steps/{step}
- POST /api/v1/auth/onboarding/skip
- GET  /api/v1/auth/user/by-username/{username} (public, no auth)

S5 把这些端点与其会话依赖收敛到 ``get_auth_session``（auth 独立 realm）后，套件的身份
须建在 auth 库 schema（``auth_db``），HTTP 走 monolith + auth 会话的 ``auth_front_client``，
使端点的 User/Profile/Onboarding 读写都落在该测量的 auth 真值 —— 与 test_auth_2fa 其余
前台认证语意用例同款收敛。
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import User
from app.modules.auth.security import create_access_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_user(auth_db: AsyncSession, username: str) -> User:
    from app.modules.auth.models import Profile

    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        account_level="normal",
    )
    auth_db.add(user)
    await auth_db.flush()
    auth_db.add(Profile(user_id=user.id, role="member"))
    await auth_db.flush()
    return user


async def _token(token_user_id: uuid.UUID) -> str:
    return create_access_token(
        user_id=token_user_id, account_level="normal", role="member"
    )


class TestOnboardingHTTP:
    async def test_default_and_put_and_get(
        self, auth_front_client: Any, auth_db: AsyncSession
    ):
        user = await _create_user(auth_db, "onbhttp")
        token = await _token(user.id)

        # default
        r = await auth_front_client.get(
            "/api/v1/auth/onboarding", headers=_auth(token)
        )
        assert r.status_code == 200
        assert r.json()["data"] == {"step": 1, "completed": False, "data": None}

        # put a step
        r = await auth_front_client.put(
            "/api/v1/auth/onboarding/steps/1",
            headers=_auth(token),
            json={"data": {"grade": "math"}},
        )
        assert r.status_code == 200
        assert r.json()["data"]["data"] == {"1": {"grade": "math"}}

        # read back
        r = await auth_front_client.get(
            "/api/v1/auth/onboarding", headers=_auth(token)
        )
        assert r.json()["data"]["step"] == 1

    async def test_skip_marks_completed(
        self, auth_front_client: Any, auth_db: AsyncSession
    ):
        user = await _create_user(auth_db, "onbskip")
        token = await _token(user.id)
        r = await auth_front_client.post(
            "/api/v1/auth/onboarding/skip", headers=_auth(token)
        )
        assert r.status_code == 200
        assert r.json()["data"]["completed"] is True
        assert r.json()["data"]["step"] == 4

    async def test_requires_auth(self, auth_front_client: Any):
        r = await auth_front_client.get("/api/v1/auth/onboarding")
        assert r.status_code in (400, 401, 403)


class TestByUsernameHTTP:
    async def test_public_lookup(
        self, auth_front_client: Any, auth_db: AsyncSession
    ):
        await _create_user(auth_db, "alice_pub")
        # no Authorization header → public access
        r = await auth_front_client.get("/api/v1/auth/user/by-username/alice_pub")
        assert r.status_code == 200
        assert r.json()["data"]["nickname"] is None

    async def test_unknown_user_errors(self, auth_front_client: Any):
        # USER_NOT_FOUND 按项目错误表映射为 401（前端任一错误即回退默认卡）
        r = await auth_front_client.get(
            "/api/v1/auth/user/by-username/does_not_exist"
        )
        assert r.status_code == 401
