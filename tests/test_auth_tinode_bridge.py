"""Tinode REST 认证桥：LKM token → Tinode 账号的首登建号与映射回写。

契约（见 auth/router_internal_tinode.py 与 deploy/tinode/tinode.conf）：
- 未配共享令牌/错令牌 fail-closed(401)，该面不成为公网入口；
- ``auth`` 无映射时回 ``newacc``（Tinode 建号），``link`` 落映射后 ``auth`` 直接回 uid；
- 账号凭据由 Tinode 管理，``gen``/``add``/``checkunique`` 一律 unsupported。
"""

import base64

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import Profile, TinodeLink, User
from auth.security import create_access_token, hashpwd
from core.config import settings
from tests.conftest import Client

_BRIDGE_TOKEN = "tinode-bridge-token-0123456789abcdef"
_URL = "/api/v1/auth/internal/tinode"


@pytest.fixture(autouse=True)
def _bridge_token():
    """每用例后清零，避免跨用例污染开关。"""
    settings.tinode_auth_token = _BRIDGE_TOKEN
    yield
    settings.tinode_auth_token = ""


def _q() -> str:
    return f"?token={_BRIDGE_TOKEN}"


def _secret(token: str) -> str:
    return base64.b64encode(f"lkm:{token}".encode()).decode()


async def _mk_user(db: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=await hashpwd("secretwxyz123"),
        account_level="normal",
    )
    db.add(user)
    await db.flush()
    db.add(Profile(user_id=user.id, role="member", nickname="Tiny"))
    await db.commit()
    return user


async def test_bridge_fail_closed_without_token(
    auth_app_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "tinode_auth_token", "")
    resp = await auth_app_client.post(f"{_URL}/auth", json={})
    assert resp.status_code == 401
    assert settings.tinode_auth_token == ""


async def test_bridge_rejects_bad_token(auth_app_client: Client) -> None:
    resp = await auth_app_client.post(f"{_URL}/auth?token=wrong", json={})
    assert resp.status_code == 401


async def test_bad_credentials_return_credentials_err(auth_app_client: Client) -> None:
    resp = await auth_app_client.post(
        f"{_URL}/auth{_q()}",
        json={"endpoint": "auth", "secret": _secret("not-a-jwt")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"err": "credentials"}


async def test_first_login_creates_account_then_link_writes_mapping(
    auth_db: AsyncSession, auth_app_client: Client
) -> None:
    user = await _mk_user(auth_db, "tinode_first")
    secret = _secret(create_access_token(user.id, "normal", "member"))

    first = await auth_app_client.post(
        f"{_URL}/auth{_q()}", json={"endpoint": "auth", "secret": secret}
    )
    assert first.status_code == 200
    body = first.json()
    assert body["rec"]["authlvl"] == "auth"
    assert "uid" not in body["rec"]
    assert body["rec"]["tags"] == [f"lkm:{user.id}"]
    assert body["newacc"]["public"]["fn"] == "Tiny"

    linked = await auth_app_client.post(
        f"{_URL}/link{_q()}",
        json={
            "endpoint": "link",
            "secret": secret,
            "rec": {"uid": "usrAbCdEfGhIjK", "authlvl": "auth"},
        },
    )
    assert linked.status_code == 200
    assert linked.json()["rec"]["uid"] == "usrAbCdEfGhIjK"

    row = (
        await auth_db.execute(select(TinodeLink).where(TinodeLink.user_id == user.id))
    ).scalars().one()
    assert row.tinode_uid == "usrAbCdEfGhIjK"

    again = await auth_app_client.post(
        f"{_URL}/auth{_q()}", json={"endpoint": "auth", "secret": secret}
    )
    assert again.json()["rec"]["uid"] == "usrAbCdEfGhIjK"
    assert "newacc" not in again.json()


async def test_link_accepts_bare_uid_from_tinode_025(
    auth_db: AsyncSession, auth_app_client: Client
) -> None:
    """Tinode 0.25 的 link 回调给裸 uid；映射原样保存，复登原样返回。"""
    user = await _mk_user(auth_db, "tinode_bare")
    secret = _secret(create_access_token(user.id, "normal", "member"))

    linked = await auth_app_client.post(
        f"{_URL}/link{_q()}",
        json={
            "endpoint": "link",
            "secret": secret,
            "rec": {"uid": "vQ3UbkFUQsQ", "authlvl": "auth"},
        },
    )
    assert linked.status_code == 200
    assert linked.json()["rec"]["uid"] == "vQ3UbkFUQsQ"

    again = await auth_app_client.post(
        f"{_URL}/auth{_q()}", json={"endpoint": "auth", "secret": secret}
    )
    assert again.json()["rec"]["uid"] == "vQ3UbkFUQsQ"
    assert "newacc" not in again.json()


async def test_link_rejects_malformed_uid(
    auth_db: AsyncSession, auth_app_client: Client
) -> None:
    user = await _mk_user(auth_db, "tinode_bad_uid")
    secret = _secret(create_access_token(user.id, "normal", "member"))
    resp = await auth_app_client.post(
        f"{_URL}/link{_q()}",
        json={
            "endpoint": "link",
            "secret": secret,
            "rec": {"uid": "not a uid!", "authlvl": "auth"},
        },
    )
    assert resp.json() == {"err": "malformed"}


async def test_account_managed_ops_are_unsupported(auth_app_client: Client) -> None:
    for op in ("gen", "add", "checkunique"):
        resp = await auth_app_client.post(f"{_URL}/{op}{_q()}", json={"endpoint": op})
        assert resp.status_code == 200
        assert resp.json() == {"err": "unsupported"}


async def test_rtagns_returns_empty_list(auth_app_client: Client) -> None:
    """Tinode 启动时会拉受限标签命名空间；缺端点会导致 30s 超时后才继续。"""
    resp = await auth_app_client.post(f"{_URL}/rtagns{_q()}", json={"endpoint": "rtagns"})
    assert resp.status_code == 200
    assert resp.json() == {"strarr": []}
