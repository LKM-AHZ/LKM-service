"""Account profile edits must survive a fresh read from the auth database."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import Profile, User
from auth.security import create_access_token, hashpwd


async def test_account_profile_round_trip(
    auth_db: AsyncSession, auth_app_client: AsyncClient
) -> None:
    user = User(
        username="profile_admin",
        hashed_password=await hashpwd("secret123456"),
        account_level="admin",
    )
    auth_db.add(user)
    await auth_db.flush()
    auth_db.add(Profile(user_id=user.id, role="super_admin", nickname="Before"))
    await auth_db.commit()

    headers = {
        "Authorization": f"Bearer {create_access_token(user.id, 'admin', 'super_admin')}"
    }
    initial = await auth_app_client.get("/api/v1/auth/me", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["data"]["username"] == "profile_admin"
    assert initial.json()["data"]["nickname"] == "Before"

    links = [{"name": "Home", "url": "https://example.com"}]
    saved = await auth_app_client.put(
        f"/api/v1/auth/{user.id}/profile",
        json={"nickname": "After", "contact_links": links},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["role"] == "super_admin"
    assert saved.json()["data"]["contact_links"] == [
        {"name": "Home", "icon": None, "url": "https://example.com"}
    ]

    auth_db.expire_all()
    fresh = await auth_app_client.get("/api/v1/auth/me", headers=headers)
    assert fresh.json()["data"]["nickname"] == "After"
    assert fresh.json()["data"]["contact_links"] == [
        {"name": "Home", "icon": None, "url": "https://example.com"}
    ]

    cleared = await auth_app_client.put(
        f"/api/v1/auth/{user.id}/profile",
        json={"nickname": None, "contact_links": []},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["nickname"] is None
    assert cleared.json()["data"]["contact_links"] == []


async def test_account_profile_rejects_unsafe_link(
    auth_db: AsyncSession, auth_app_client: AsyncClient
) -> None:
    user = User(
        username="profile_links",
        hashed_password=await hashpwd("secret123456"),
        account_level="normal",
    )
    auth_db.add(user)
    await auth_db.flush()
    auth_db.add(Profile(user_id=user.id, role="member"))
    await auth_db.commit()
    token = create_access_token(user.id, "normal", "member")
    response = await auth_app_client.put(
        f"/api/v1/auth/{user.id}/profile",
        json={"contact_links": [{"name": "Bad", "url": "javascript:alert(1)"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
