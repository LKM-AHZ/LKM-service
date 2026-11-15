"""Real JWT verification and provider wire-contract regression checks."""

import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.modules.notification.huawei import HuaweiPushError, send_notification
from core.config import Settings


def configured() -> tuple[Settings, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return Settings(
        _env_file=None,
        huawei_push_enabled=True,
        huawei_push_project_id="123",
        huawei_push_key_id="test-kid",
        huawei_push_sub_account="test-issuer",
        huawei_push_private_key=pem,
    ), key


@pytest.mark.parametrize("code", ["80000000", "80100000", "80200001"])
async def test_v3_wire_and_errors(code: str) -> None:
    config, key = configured()

    def handle(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url) == "https://push-api.cloud.huawei.com/v3/123/messages:send"
        )
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token,
            key.public_key(),
            algorithms=["PS256"],
            audience="https://oauth-login.cloud.huawei.com/oauth2/v3/token",
            issuer="test-issuer",
        )
        assert claims["exp"] - claims["iat"] == 3600
        assert jwt.get_unverified_header(token)["kid"] == "test-kid"
        assert request.headers["push-type"] == "0"
        data = json.loads(request.content)
        assert data["target"]["token"] == ["huawei-token"]
        assert data["payload"]["notification"]["clickAction"] == {"actionType": 0}
        return httpx.Response(200, json={"code": code, "msg": "ignored"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if code == "80000000":
            await send_notification(client, ["huawei-token"], {}, config=config)
        else:
            with pytest.raises(HuaweiPushError, match=code):
                await send_notification(client, ["huawei-token"], {}, config=config)


async def test_disabled_has_no_network() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(HuaweiPushError, match="not configured"):
            await send_notification(
                client, ["token"], {}, config=Settings(_env_file=None)
            )


def test_partial_configuration_rejected() -> None:
    with pytest.raises(ValueError, match="Huawei Push requires"):
        Settings(_env_file=None, huawei_push_enabled=True)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
    ],
)
async def test_transport_and_malformed_response_not_success(
    response: httpx.Response,
) -> None:
    config, _ = configured()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises((HuaweiPushError, httpx.HTTPStatusError)):
            await send_notification(client, ["token"], {}, config=config)


async def test_unconfigured_registration_is_explicitly_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from app.modules.notification import service
    from core.err import BizError, CommonErr

    monkeypatch.setattr(service.settings, "huawei_push_enabled", False)
    with pytest.raises(BizError) as failure:
        await service.register_token(None, uuid.uuid4(), "token", "huawei")
    assert failure.value.errcode == CommonErr.UNAVAILABLE
