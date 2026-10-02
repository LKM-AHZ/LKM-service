"""RS256-only：签发/验签只走非对称，HS256 兼容已移除。

蓝图 §4.2 的目标态：AUTH 持私钥签发、主服务与网关持公钥验签。本组用例锁定四条契约：

1. **只签发 RS256**：任何 token 的头部 ``alg`` 都是 RS256（无 HS 降级路径）；
2. **只接受 RS256**：HS256（含 alg confusion 手工伪造）与 ``none`` 一律在选算法阶段拒；
3. **签发需私钥、验签只持公钥亦可**：未配私钥即 ``RuntimeError``；
4. **aud 三套隔离不回退**：``lkm:web`` / ``lkm:temp`` / ``lkm:admin`` 互不通用。
"""

import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from auth import jwt_keys
from auth.admin_session import (
    create_admin_access_token,
    decode_admin_access,
)
from auth.security import (
    create_access_token,
    create_temp_token,
    decode_access_token,
    decode_temp_token,
)
from core.config import settings

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = (
    _PRIVATE_KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)


def _access(user_id: uuid.UUID) -> str:
    return create_access_token(user_id=user_id, account_level="normal", role="member")


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ─────────────────────── 签发：恒为 RS256 ───────────────────────


def test_access_token_is_always_rs256() -> None:
    user_id = uuid.uuid4()
    token = _access(user_id)

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    payload = decode_access_token(token)
    assert payload["user_id"] == str(user_id)
    assert payload["aud"] == "lkm:web"
    assert payload["type"] == "access"


def test_encode_requires_private_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配私钥即无法签发（无 HS 回落）。"""
    monkeypatch.setattr(settings, "jwt_private_key", None)
    monkeypatch.setattr(settings, "jwt_public_key", None)
    with pytest.raises(RuntimeError, match="需要 RSA 私钥"):
        _access(uuid.uuid4())


# ─────────────────────── 验签：只持公钥的验签方 ───────────────────────


def test_verify_with_public_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """验签方部署：只给公钥（无私钥）也能验 AUTH 签发的 RS256 token，但不能签发。"""
    monkeypatch.setattr(settings, "jwt_private_key", None)
    monkeypatch.setattr(settings, "jwt_public_key", SecretStr(_PUBLIC_PEM))
    rs_token = jwt.encode(
        {
            "user_id": str(uuid.uuid4()),
            "type": "access",
            "aud": "lkm:web",
            "iat": int(time.time()),
            "exp": 4102444800,
        },
        _PRIVATE_KEY,
        algorithm="RS256",
    )
    assert decode_access_token(rs_token)["type"] == "access"


# ─────────────────────── HS256 / 非 RS256 一律拒 ───────────────────────


def test_hs256_token_rejected() -> None:
    hs_token = jwt.encode(
        {
            "user_id": str(uuid.uuid4()),
            "type": "access",
            "aud": "lkm:web",
            "exp": 4102444800,
        },
        "x" * 40,
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(hs_token)


def test_alg_confusion_rejected() -> None:
    """把**公钥**当 HMAC 密钥签出来的 HS256 token 不得被接受（alg confusion）。

    PyJWT 自身拒绝用 PEM 作 HMAC 密钥，故这里手工拼 token（header.payload|HMAC）——
    模拟真实攻击者绕过客户端库的情形。验的是**我们读侧的 alg 分支**：``alg != RS256``
    即在签名比对之前拒。
    """
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64u(
        json.dumps(
            {
                "user_id": str(uuid.uuid4()),
                "type": "access",
                "aud": "lkm:web",
                "exp": 4102444800,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(_PUBLIC_PEM.encode(), signing_input, hashlib.sha256).digest()
    forged = f"{header}.{payload}.{_b64u(sig)}"

    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(forged)


def test_none_alg_rejected() -> None:
    unsigned = jwt.encode(
        {"user_id": str(uuid.uuid4()), "type": "access", "aud": "lkm:web"},
        key="",
        algorithm="none",
    )
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(unsigned)


# ─────────────────────── aud 三套隔离 ───────────────────────


def test_audiences_stay_isolated() -> None:
    user_id = uuid.uuid4()
    temp = create_temp_token(user_id, purpose="2fa")
    assert decode_temp_token(temp)["aud"] == "lkm:temp"
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(temp)

    access = _access(user_id)
    with pytest.raises(jwt.InvalidAudienceError):
        decode_temp_token(access)


def test_admin_token_uses_rs256_and_admin_aud() -> None:
    class _User:
        id = uuid.uuid4()
        account_level = "admin"
        token_version = 3

    token = create_admin_access_token(_User())
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    payload = decode_admin_access(token)
    assert payload["aud"] == "lkm:admin"
    assert payload["token_version"] == 3
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(token)


# ─────────────────────── JWKS ───────────────────────


def test_jwks_document_matches_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_private_key", SecretStr(_PRIVATE_PEM))
    monkeypatch.setattr(settings, "jwt_public_key", None)
    doc = jwt_keys.jwks_document()
    assert len(doc["keys"]) == 1
    key = doc["keys"][0]
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert "key_ops" not in key

    numbers = _PRIVATE_KEY.public_key().public_numbers()

    def _b64uint(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    assert key["e"] == _b64uint(numbers.e)
    assert key["n"] == _b64uint(numbers.n)

    # kid = RFC 7638 thumbprint：去掉 kid 后重算应一致（换钥即变，可做轮换标识）
    core = {"e": key["e"], "kty": key["kty"], "n": key["n"]}
    canonical = json.dumps(core, separators=(",", ":"), sort_keys=True).encode()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(canonical).digest())
        .rstrip(b"=")
        .decode()
    )
    assert key["kid"] == expected


def test_jwks_empty_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_private_key", None)
    monkeypatch.setattr(settings, "jwt_public_key", None)
    assert jwt_keys.jwks_document() == {"keys": []}


async def test_jwks_endpoint_published(auth_app_client) -> None:
    """端点挂在站点根（不走 api_prefix），配了密钥即发布单把 RSA 公钥。"""
    resp = await auth_app_client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert len(keys) == 1 and keys[0]["kty"] == "RSA"
