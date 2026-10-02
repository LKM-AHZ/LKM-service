"""JWT claim and refresh-session binding regression tests without a database."""

import datetime
import json
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from pydantic import SecretStr, ValidationError

from auth import admin_router, jwt_keys, router, service_auth
from auth.deps import CurrentUser
from auth.errors import AuthErr
from auth.schemas import RefreshRequest
from auth.security import create_access_token, decode_access_token
from core.config import settings
from core.err import BizError


@pytest.fixture(autouse=True)
def signing_keys(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    monkeypatch.setattr(settings, "jwt_private_key", SecretStr(private))
    monkeypatch.setattr(settings, "jwt_public_key", SecretStr(public))
    monkeypatch.setattr(settings, "jwt_private_key_file", "")
    monkeypatch.setattr(settings, "jwt_public_key_file", "")


@pytest.mark.parametrize("missing", ["aud", "exp", "iat"])
def test_jwt_requires_core_claims(missing):
    payload = {
        "type": "access",
        "aud": "lkm:web",
        "exp": int(time.time()) + 60,
        "iat": int(time.time()),
    }
    del payload[missing]
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(jwt_keys.encode(payload))


def test_jwt_rejects_oversized_input():
    with pytest.raises(jwt.DecodeError, match="too large"):
        decode_access_token("x" * 8193)


@pytest.mark.asyncio
async def test_issued_access_is_bound_to_refresh_token(monkeypatch):
    stored = {}

    async def store(db, user_id, raw, **kwargs):
        stored["raw"] = raw
        return datetime.datetime.now(datetime.UTC)

    monkeypatch.setattr(service_auth, "store_refresh_token", store)
    user = SimpleNamespace(
        id=uuid.uuid4(),
        account_level="normal",
        token_version=0,
        profile=SimpleNamespace(role="member"),
    )
    access, refresh = await service_auth.issue_session_tokens(object(), user)
    assert refresh == stored["raw"]
    assert decode_access_token(access)["rt_hash"] == service_auth.hash_refresh_token(
        refresh
    )


@pytest.mark.asyncio
async def test_web_role_activation_rejects_other_session(monkeypatch):
    class NoDb:
        async def scalar(self, *args):
            pytest.fail("cross-session token reached database lookup")

    monkeypatch.setattr(
        router,
        "decode_access_token",
        lambda token: {"rt_hash": service_auth.hash_refresh_token("session-a")},
    )
    current = CurrentUser(id=uuid.uuid4(), account_level="normal", role="member")
    with pytest.raises(BizError):
        await router.activate_roles(
            router._ActivateRolesRequest(roles=["member"], refresh_token="session-b"),
            "access-a",
            current,
            NoDb(),
        )


@pytest.mark.asyncio
async def test_web_role_activation_keeps_session_expiry(monkeypatch):
    refresh = "session-a"
    refresh_hash = service_auth.hash_refresh_token(refresh)
    user_id = uuid.uuid4()
    expiry = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=90)
    stored = SimpleNamespace(expires_at=expiry, active_roles=None)

    class Db:
        async def scalar(self, *args):
            return stored

    class Roles:
        def __init__(self, db):
            pass

        async def list_roles(self, user_id):
            return []

    monkeypatch.setattr(router, "UserRoleRepository", Roles)
    old = create_access_token(
        user_id, "normal", "member", refresh_token_hash=refresh_hash
    )
    current = CurrentUser(id=user_id, account_level="normal", role="member")
    response = await router.activate_roles(
        router._ActivateRolesRequest(roles=["normal:member"], refresh_token=refresh),
        old,
        current,
        Db(),
    )
    assert response.status_code == 200
    assert stored.active_roles == ["normal:member"]
    issued = decode_access_token(json.loads(response.body)["data"]["access_token"])
    assert issued["rt_hash"] == refresh_hash
    assert issued["exp"] <= int(expiry.timestamp())


@pytest.mark.asyncio
async def test_admin_role_activation_rejects_other_session(monkeypatch):
    async def current(*args):
        return SimpleNamespace(id=uuid.uuid4())

    class NoDb:
        async def scalar(self, *args):
            pytest.fail("cross-session token reached database lookup")

    monkeypatch.setattr(admin_router, "_require_admin_from_cookie", current)
    monkeypatch.setattr(
        admin_router.jwt_keys,
        "decode",
        lambda *args, **kwargs: {
            "rt_hash": service_auth.hash_refresh_token("session-a")
        },
    )
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"cookie", b"admin_session=access-a; admin_refresh=session-b")
            ],
        }
    )
    with pytest.raises(BizError):
        await admin_router.activate_admin_roles(
            admin_router._AdminActivateRolesRequest(roles=["admin:super_admin"]),
            request,
            NoDb(),
        )


@pytest.mark.asyncio
async def test_admin_stepup_rejects_other_session_before_totp(monkeypatch):
    async def current(*args):
        return SimpleNamespace(id=uuid.uuid4())

    async def verify(*args):
        pytest.fail("TOTP was consumed for mismatched session")

    monkeypatch.setattr(admin_router, "_require_admin_from_cookie", current)
    monkeypatch.setattr(admin_router, "verify_user_totp", verify)
    monkeypatch.setattr(
        admin_router.jwt_keys,
        "decode",
        lambda *args, **kwargs: {
            "rt_hash": service_auth.hash_refresh_token("session-a")
        },
    )
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"cookie", b"admin_session=access-a; admin_refresh=session-b")
            ],
        }
    )
    with pytest.raises(BizError):
        await admin_router.admin_verify_2fa(
            admin_router._AdminVerify2FARequest(code="123456"), request, object()
        )


@pytest.mark.asyncio
async def test_refresh_input_is_bounded_before_hashing():
    oversized = "x" * 129
    with pytest.raises(ValidationError):
        RefreshRequest(refresh_token=oversized)
    with pytest.raises(BizError) as exc:
        await service_auth.refresh_access_token(object(), oversized)
    assert exc.value.errcode == AuthErr.TOKEN_INVALID
