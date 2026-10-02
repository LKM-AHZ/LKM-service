"""Session rotation invariants without a PostgreSQL fixture."""

import datetime
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Request

from auth import admin_router, admin_session, security, service_auth
from auth.errors import AuthErr
from auth.models import RefreshToken
from core.err import BizError


def _stored_token(*, mfa_at=None):
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=2),
        mfa_verified=mfa_at is not None,
        mfa_at=mfa_at,
        active_roles=None,
    )


def test_access_tokens_cannot_outlive_refresh_session(monkeypatch):
    captured = []

    def encode(payload):
        captured.append(payload)
        return "jwt"

    monkeypatch.setattr(security.jwt_keys, "encode", encode)
    expires_at = int(time.time()) + 30
    security.create_access_token(
        uuid.uuid4(), "normal", "member", session_expires_at=expires_at
    )
    user = SimpleNamespace(id=uuid.uuid4(), account_level="admin", token_version=0)
    admin_session.create_admin_access_token(
        user,
        session_expires_at=datetime.datetime.fromtimestamp(expires_at, datetime.UTC),
    )
    assert [payload["exp"] for payload in captured] == [expires_at, expires_at]


@pytest.mark.asyncio
async def test_web_refresh_keeps_expiry_and_mfa_origin(monkeypatch):
    mfa_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20)
    stored = _stored_token(mfa_at=mfa_at)
    user = SimpleNamespace(is_locked=False, locked_until=None)
    issued = {}

    async def consume(*args, **kwargs):
        return True

    async def get(*args, **kwargs):
        return stored

    async def issue(*args, **kwargs):
        issued.update(kwargs)
        return "access", "new-refresh"

    class Users:
        def __init__(self, db):
            pass

        async def get_with_profile_or_raise(self, *args):
            return user

    monkeypatch.setattr(service_auth, "consume_once", consume)
    monkeypatch.setattr(service_auth, "get_or_raise", get)
    monkeypatch.setattr(service_auth, "UserRepository", Users)
    monkeypatch.setattr(service_auth, "issue_session_tokens", issue)

    result = await service_auth.refresh_access_token(object(), "old-refresh")
    assert result == {"access_token": "access", "refresh_token": "new-refresh"}
    assert issued["session_expires_at"] == stored.expires_at
    assert issued["mfa_at"] == mfa_at
    assert issued["mfa_verified"] is True


@pytest.mark.asyncio
async def test_web_refresh_rejects_locked_account(monkeypatch):
    stored = _stored_token()
    user = SimpleNamespace(
        is_locked=True,
        locked_until=datetime.datetime.now(datetime.UTC)
        + datetime.timedelta(minutes=10),
    )

    async def consume(*args, **kwargs):
        return True

    async def get(*args, **kwargs):
        return stored

    class Users:
        def __init__(self, db):
            pass

        async def get_with_profile_or_raise(self, *args):
            return user

    monkeypatch.setattr(service_auth, "consume_once", consume)
    monkeypatch.setattr(service_auth, "get_or_raise", get)
    monkeypatch.setattr(service_auth, "UserRepository", Users)

    with pytest.raises(BizError) as exc:
        await service_auth.refresh_access_token(object(), "old-refresh")
    assert exc.value.errcode == AuthErr.ACCOUNT_LOCKED


@pytest.mark.asyncio
async def test_admin_refresh_keeps_expiry_without_reinstating_missing_mfa_time(
    monkeypatch,
):
    stored = _stored_token()
    stored.mfa_verified = True  # Legacy row written without its verification time.
    user = SimpleNamespace(
        id=stored.user_id,
        username="admin",
        account_level="admin",
        created_at=None,
        is_locked=False,
        locked_until=None,
    )
    inserted = []
    signed = {}

    class Db:
        def add(self, row):
            inserted.append(row)

        async def commit(self):
            pass

    async def limit(*args, **kwargs):
        pass

    async def consume(*args, **kwargs):
        return True

    async def get(db, model, *args):
        return stored if model is RefreshToken else user

    def sign(*args, **kwargs):
        signed.update(kwargs)
        return "new-access"

    monkeypatch.setattr(admin_router, "check_code_rate_limit", limit)
    monkeypatch.setattr(admin_router, "consume_once", consume)
    monkeypatch.setattr(admin_router, "get_or_raise", get)
    monkeypatch.setattr(admin_router, "create_admin_access_token", sign)
    request = Request(
        {"type": "http", "headers": [(b"cookie", b"admin_refresh=old-refresh")]}
    )

    response = await admin_router.admin_refresh(request, Db())
    assert response.status_code == 200
    assert inserted[0].expires_at == stored.expires_at
    assert inserted[0].mfa_verified is False
    assert signed["mfa_verified"] is False
    assert signed["refresh_token_hash"] == inserted[0].token_hash


@pytest.mark.asyncio
async def test_admin_stepup_persists_verification_time(monkeypatch):
    user = SimpleNamespace(
        id=uuid.uuid4(), username="admin", account_level="admin", created_at=None
    )
    stored = SimpleNamespace(
        mfa_verified=False,
        mfa_at=None,
        active_roles=None,
        revoked_at=None,
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2),
    )

    class Result:
        def scalars(self):
            return self

        def first(self):
            return stored

    class Db:
        async def execute(self, statement):
            return Result()

        async def commit(self):
            pass

    async def current(*args):
        return user

    async def verify(*args):
        pass

    async def roles(*args):
        return ("admin:super_admin",)

    monkeypatch.setattr(admin_router, "_require_admin_from_cookie", current)
    monkeypatch.setattr(admin_router, "verify_user_totp", verify)
    monkeypatch.setattr(admin_router, "_active_cookie_roles", roles)
    monkeypatch.setattr(
        admin_router.jwt_keys,
        "decode",
        lambda *a, **k: {"rt_hash": admin_router.hash_refresh_token("old-refresh")},
    )
    signed = {}

    def sign(*args, **kwargs):
        signed.update(kwargs)
        return "jwt"

    monkeypatch.setattr(admin_router, "create_admin_access_token", sign)
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"cookie", b"admin_refresh=old-refresh; admin_session=access")
            ],
        }
    )

    response = await admin_router.admin_verify_2fa(
        admin_router._AdminVerify2FARequest(code="123456"), request, Db()
    )
    assert response.status_code == 200
    assert stored.mfa_verified is True
    assert stored.active_roles == ["admin:super_admin"]
    assert stored.mfa_at is not None
    assert signed["session_expires_at"] == stored.expires_at
    assert signed["refresh_token_hash"] == admin_router.hash_refresh_token(
        "old-refresh"
    )
    assert (
        abs((datetime.datetime.now(datetime.UTC) - stored.mfa_at).total_seconds()) < 5
    )
