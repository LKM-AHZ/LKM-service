"""批 5：RS256 签发 + JWKS 发布 + HS/RS 双验签灰度。

蓝图 §4.2 的演进目标：AUTH 持私钥签发、主服务与网关持公钥验签。本组用例锁定四条契约：

1. **算法由密钥决定**：配了私钥即 RS256，未配沿用 HS256（本地/测试默认路径不受影响）；
2. **按 alg 分支验签**：RS256 只用公钥、HS256 只用共享密钥（不做「同一密钥按算法列表试」，
   避免 alg confusion）；
3. **灰度开关**：``jwt_hs_fallback`` 关掉后 HS256 token 一律拒，RS256 照常；
4. **aud 三套隔离不回退**：``lkm:web`` / ``lkm:temp`` / ``lkm:admin`` 互不通用。
"""

import base64
import json
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from app.core.config import settings
from app.core.secrets import reveal
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

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


@pytest.fixture
def rs256(monkeypatch: pytest.MonkeyPatch) -> None:
    """切到 RS256 签发（公钥由私钥推导），并保留 HS 灰度开关由用例自行设定。"""
    monkeypatch.setattr(settings, "jwt_private_key", SecretStr(_PRIVATE_PEM))
    monkeypatch.setattr(settings, "jwt_public_key", None)


def _access(user_id: uuid.UUID) -> str:
    return create_access_token(user_id=user_id, account_level="normal", role="member")


# ─────────────────────── 算法选择 ───────────────────────


def test_defaults_to_hs256_without_key() -> None:
    assert settings.jwt_private_key is None
    assert jwt_keys.signing_algorithm() == "HS256"
    token = _access(uuid.uuid4())
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    assert decode_access_token(token)["type"] == "access"


def test_private_key_switches_to_rs256(rs256: None) -> None:
    assert jwt_keys.signing_algorithm() == "RS256"
    user_id = uuid.uuid4()
    token = _access(user_id)

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    payload = decode_access_token(token)
    assert payload["user_id"] == str(user_id)
    assert payload["aud"] == "lkm:web"


def test_rs256_signed_with_public_key_alone(rs256: None, monkeypatch) -> None:
    """验签方部署：只给公钥（无私钥）也能验，但不签发。"""
    monkeypatch.setattr(settings, "jwt_private_key", None)
    monkeypatch.setattr(
        settings,
        "jwt_public_key",
        SecretStr(
            _PRIVATE_KEY.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        ),
    )
    assert jwt_keys.signing_algorithm() == "HS256"  # 无私钥 → 仍是 HS 签发
    # 手工造一个 RS256 token（模拟 AUTH 签发的），主服务侧可验
    rs_token = jwt.encode(
        {
            "user_id": str(uuid.uuid4()),
            "type": "access",
            "aud": "lkm:web",
            "exp": 4102444800,
        },
        _PRIVATE_KEY,
        algorithm="RS256",
    )
    assert decode_access_token(rs_token)["type"] == "access"


# ─────────────────────── 双验签灰度 ───────────────────────


def test_hs_token_accepted_while_fallback_on(rs256: None) -> None:
    hs_token = jwt.encode(
        {
            "user_id": str(uuid.uuid4()),
            "type": "access",
            "aud": "lkm:web",
            "exp": 4102444800,
        },
        reveal(settings.jwt_secret),
        algorithm="HS256",
    )
    assert decode_access_token(hs_token)["aud"] == "lkm:web"


def test_hs_token_rejected_once_fallback_off(
    rs256: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切换后：HS256 旧 token 被拒，RS256 照常——即「关 HS」的验收口径。"""
    hs_token = jwt.encode(
        {
            "user_id": str(uuid.uuid4()),
            "type": "access",
            "aud": "lkm:web",
            "exp": 4102444800,
        },
        reveal(settings.jwt_secret),
        algorithm="HS256",
    )
    monkeypatch.setattr(settings, "jwt_hs_fallback", False)

    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(hs_token)
    assert decode_access_token(_access(uuid.uuid4()))["type"] == "access"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_alg_confusion_rejected(rs256: None) -> None:
    """把**公钥**当 HMAC 密钥签出来的 HS256 token 不得被接受（alg confusion）。

    PyJWT 自身拒绝用 PEM 作 HMAC 密钥，故这里手工拼 token（header.payload|HMAC）——
    模拟真实攻击者绕过客户端库的情形，验的是**我们读侧的 alg 分支**而非库的拦截。
    """
    import hashlib
    import hmac

    public_pem = (
        _PRIVATE_KEY.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
        .encode()
    )
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
    sig = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    forged = f"{header}.{payload}.{_b64u(sig)}"

    # 灰度开（默认）：HS 路径会用 jwt_secret 验签 → 签名不匹配
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(forged)


def test_alg_confusion_rejected_when_fallback_off(
    rs256: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 HS 回退后，任何 HS256 token（含混淆铸造的）在选算法阶段即拒。"""
    monkeypatch.setattr(settings, "jwt_hs_fallback", False)
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64u(json.dumps({"aud": "lkm:web", "type": "access"}).encode())
    forged = f"{header}.{payload}.{_b64u(b'x' * 32)}"
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(forged)


def test_none_alg_rejected(rs256: None) -> None:
    unsigned = jwt.encode(
        {"user_id": str(uuid.uuid4()), "type": "access", "aud": "lkm:web"},
        key="",
        algorithm="none",
    )
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(unsigned)


# ─────────────────────── aud 三套隔离 ───────────────────────


def test_audiences_stay_isolated(rs256: None) -> None:
    user_id = uuid.uuid4()
    temp = create_temp_token(user_id, purpose="2fa")
    assert decode_temp_token(temp)["aud"] == "lkm:temp"
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(temp)

    access = _access(user_id)
    with pytest.raises(jwt.InvalidAudienceError):
        decode_temp_token(access)


def test_admin_token_uses_rs256_and_admin_aud(rs256: None) -> None:
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


def test_jwks_document_matches_public_key(rs256: None) -> None:
    doc = jwt_keys.jwks_document()
    assert len(doc["keys"]) == 1
    key = doc["keys"][0]
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert "key_ops" not in key

    numbers = _PRIVATE_KEY.public_key().public_numbers()

    def _b64u(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    assert key["e"] == _b64u(numbers.e)
    assert key["n"] == _b64u(numbers.n)

    # kid = RFC 7638 thumbprint：去掉 kid 后重算应一致（换钥即变，可做轮换标识）
    core = {"e": key["e"], "kty": key["kty"], "n": key["n"]}
    canonical = json.dumps(core, separators=(",", ":"), sort_keys=True).encode()
    import hashlib

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(canonical).digest())
        .rstrip(b"=")
        .decode()
    )
    assert key["kid"] == expected


def test_jwks_empty_without_keys() -> None:
    assert jwt_keys.jwks_document() == {"keys": []}


async def test_jwks_endpoint_published(
    auth_app_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端点挂在站点根（不走 api_prefix），配了密钥即发布公钥。"""
    resp = await auth_app_client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    assert resp.json() == {"keys": []}

    monkeypatch.setattr(settings, "jwt_private_key", SecretStr(_PRIVATE_PEM))
    monkeypatch.setattr(settings, "jwt_public_key", None)
    resp = await auth_app_client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert len(keys) == 1 and keys[0]["kty"] == "RSA"


# ─────────────────────── 配置自洽 ───────────────────────


def test_fallback_off_without_any_key_is_rejected() -> None:
    """关掉 HS 回退却没有任何 RSA 公钥 → 两条验签路径都不通，属自相矛盾配置。"""
    from app.core.config import Settings

    with pytest.raises(ValueError, match="jwt_hs_fallback=false"):
        Settings(
            env="production",
            jwt_secret=SecretStr("x" * 40),
            totp_encryption_key=SecretStr("y" * 40),
            verification_code_pepper=SecretStr("z" * 40),
            jwt_hs_fallback=False,
        )
